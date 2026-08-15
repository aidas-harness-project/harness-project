"""An unapproved medical policy cannot enforce a gate nobody can clear.

Found by running CASE_027 -- the first run ever to reach `claim_analysis`
with the restored gate live. Two individually sound decisions met and formed a
dead end:

  * `_medical_clearance_required` gates `claim_analysis` unconditionally,
    because the medical variables are an INPUT to that stage (Rekhet,
    2026-08-10, deliberate and preserved here).
  * the shipped operator policy is `operations_enabled: false`, `actors: []`,
    `approval: null` -- the approved state per
    `docs/medical-appropriateness-screening-deferrals.md`, whose 18 deferrals
    are all still open.

`record-medical-referral-decision` is the only route to the non-blocking
`do_not_refer` state and it authenticates through `operator_auth`, which fails
closed on that policy. So an enforced run blocked `claim_analysis` forever,
for every case, with no action any human could take. A gate nothing can
satisfy is not satisfied, it is bypassed -- the failure that retired clause
normalization on 2026-08-15.
"""
import json

import dao
import operator_auth


def _write_policy(tmp_path, monkeypatch, **overrides):
    policy = {
        "policy_version": "operator_auth_policy.v0.1",
        "schema_version": "0.1",
        "actors": [],
        "operations_enabled": False,
        "approval": None,
    }
    policy.update(overrides)
    path = tmp_path / "operator_auth_policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(operator_auth, "POLICY_PATH", path)
    return path


ENFORCED = {"medical_gate_status": "enforced"}


def test_shipped_policy_is_unapproved(tmp_path, monkeypatch):
    """The real file, not a fixture. If this ever fails the deferrals were
    resolved and the rest of this module's premise changed."""
    assert dao._medical_operations_approved() is False


def test_enforced_run_is_not_gated_while_the_policy_is_unapproved(
        tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)
    assert dao._medical_gate_applies(ENFORCED) is False
    assert dao._medical_clearance_required(
        ENFORCED, "claim_analysis", status="passed") is False
    assert dao._medical_clearance_required(
        ENFORCED, "claim_analysis", snapshot=True) is False


def test_approving_the_policy_restores_the_gate_with_no_code_change(
        tmp_path, monkeypatch):
    """The property that makes yielding safe. Without this the change would be
    a permanent hole rather than a deferral of enforcement."""
    _write_policy(
        tmp_path, monkeypatch,
        operations_enabled=True,
        approval={"approved_by": "medical lead", "effective_from": "2026-09-01"},
        actors=[{"actor_id": "a1"}])
    assert dao._medical_operations_approved() is True
    assert dao._medical_gate_applies(ENFORCED) is True
    assert dao._medical_clearance_required(
        ENFORCED, "claim_analysis", status="passed") is True


def test_half_approved_policy_does_not_activate(tmp_path, monkeypatch):
    """Both halves are required, matching every other consumer's rule
    (`not operations_enabled or approval is None`). A switch flipped without
    an approval record is not an approval."""
    _write_policy(tmp_path, monkeypatch, operations_enabled=True)
    assert dao._medical_operations_approved() is False

    _write_policy(tmp_path, monkeypatch,
                  approval={"approved_by": "x", "effective_from": "2026-09-01"})
    assert dao._medical_operations_approved() is False


def test_unreadable_policy_fails_closed_toward_unapproved(
        tmp_path, monkeypatch):
    """Direction matters: unreadable means clearance is unreachable, so
    treating it as approved would reinstate the permanent block."""
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(operator_auth, "POLICY_PATH", missing)
    assert dao._medical_operations_approved() is False

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(operator_auth, "POLICY_PATH", broken)
    assert dao._medical_operations_approved() is False

    listy = tmp_path / "listy.json"
    listy.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(operator_auth, "POLICY_PATH", listy)
    assert dao._medical_operations_approved() is False


def test_never_evaluated_is_still_ungated_under_an_approved_policy(
        tmp_path, monkeypatch):
    """Approving the policy must not retro-fail the 53 pre-restore runs -- the
    2026-08-14 forward-scoping decision is independent of this change."""
    _write_policy(
        tmp_path, monkeypatch,
        operations_enabled=True,
        approval={"approved_by": "medical lead", "effective_from": "2026-09-01"})
    assert dao._medical_gate_applies(
        {"medical_gate_status": "never_evaluated"}) is False
    assert dao._medical_gate_applies({}) is False
