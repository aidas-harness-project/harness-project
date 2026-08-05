"""Narrow authenticated-operator prerequisite for medical review."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import operator_auth  # pyright: ignore[reportMissingImports]


def _enabled_policy(tmp_path: Path, token: str) -> Path:
    path = tmp_path / "operator-policy.json"
    path.write_text(json.dumps({
        "schema_version": "operator_auth_policy.v0.1",
        "policy_version": "operator_auth_policy.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic medical-review tests only",
        },
        "actors": [{
            "actor_id": "OP_MEDICAL_TEST",
            "display_name": "Synthetic Medical Coordinator",
            "role": "medical_coordinator",
            "medical_actor_id": "coordinator-1",
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "allowed_actions": ["medical_review"],
        }],
    }), encoding="utf-8")
    return path


def test_shipped_operator_policy_is_disabled_and_unapproved():
    policy = json.loads(operator_auth.POLICY_PATH.read_text(encoding="utf-8"))
    assert policy["operations_enabled"] is False
    assert policy["approval"] is None
    assert policy["actors"] == []
    with pytest.raises(operator_auth.OperatorAuthorizationError, match="disabled"):
        operator_auth.authenticate_token("synthetic-token", "medical_review")


def test_approved_token_returns_server_configured_medical_identity(
    tmp_path: Path, monkeypatch
):
    token = "synthetic-medical-token"
    monkeypatch.setattr(operator_auth, "POLICY_PATH", _enabled_policy(tmp_path, token))

    identity = operator_auth.authenticate_token(token, "medical_review")

    assert identity == {
        "actor_id": "OP_MEDICAL_TEST",
        "display_name": "Synthetic Medical Coordinator",
        "role": "medical_coordinator",
        "policy_version": "operator_auth_policy.v0.1",
        "authentication_method": "bearer_sha256_policy",
        "medical_actor_id": "coordinator-1",
    }
    assert operator_auth.token_from_header(f"Bearer {token}") == token


@pytest.mark.parametrize(
    "token,action,claimed_name",
    [
        ("wrong-token", "medical_review", None),
        ("synthetic-medical-token", "resume_pipeline", None),
        ("synthetic-medical-token", "medical_review", "Spoofed Operator"),
    ],
)
def test_token_action_and_claimed_display_all_fail_closed(
    tmp_path: Path, monkeypatch, token, action, claimed_name
):
    approved = "synthetic-medical-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_policy(tmp_path, approved),
    )
    with pytest.raises(operator_auth.OperatorAuthorizationError):
        operator_auth.authenticate_token(
            token,
            action,
            claimed_display_name=claimed_name,
        )


def test_policy_symlink_is_rejected(tmp_path: Path, monkeypatch):
    real = _enabled_policy(tmp_path, "synthetic-medical-token")
    alias = tmp_path / "policy-alias.json"
    alias.symlink_to(real)
    monkeypatch.setattr(operator_auth, "POLICY_PATH", alias)

    with pytest.raises(operator_auth.OperatorAuthorizationError):
        operator_auth.authenticate_token(
            "synthetic-medical-token",
            "medical_review",
        )


def test_duplicate_token_digest_is_rejected_before_actor_selection(
    tmp_path: Path, monkeypatch
):
    token = "synthetic-medical-token"
    path = _enabled_policy(tmp_path, token)
    policy = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(policy["actors"][0])
    duplicate.update({
        "actor_id": "OP_MEDICAL_DUPLICATE",
        "display_name": "Synthetic Duplicate Operator",
        "medical_actor_id": "coordinator-duplicate",
    })
    policy["actors"].append(duplicate)
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(operator_auth, "POLICY_PATH", path)

    with pytest.raises(
        operator_auth.OperatorAuthorizationError,
        match="duplicate token digest",
    ):
        operator_auth.authenticate_token(token, "medical_review")
