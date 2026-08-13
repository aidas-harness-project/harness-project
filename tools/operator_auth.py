"""Fail-closed named-operator bearer-token policy for medical review.

Only token SHA-256 digests are stored in repository configuration. Raw bearer
values arrive through an HTTP Authorization header or the per-process
AIDAS_OPERATOR_TOKEN environment variable and are never logged or persisted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "operators" / "operator_auth_policy_v0.1.json"
POLICY_SCHEMA_PATH = ROOT / "schemas" / "operator_auth_policy.schema.json"
TOKEN_ENV = "AIDAS_OPERATOR_TOKEN"


class OperatorAuthorizationError(ValueError):
    """Raised when operator identity or policy cannot be trusted."""


def _load_policy() -> dict:
    try:
        descriptor = os.open(POLICY_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OperatorAuthorizationError(
            "operator authorization policy is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OperatorAuthorizationError(
                "operator authorization policy is not a regular file"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            policy = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorAuthorizationError(
            "operator authorization policy is invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        schema = json.loads(POLICY_SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(policy)
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorAuthorizationError(
            "operator authorization policy schema is unavailable"
        ) from exc
    if errors:
        raise OperatorAuthorizationError(
            "operator authorization policy is invalid"
        )
    actors = policy.get("actors")
    if isinstance(actors, list):
        actor_ids = [actor.get("actor_id") for actor in actors if isinstance(actor, dict)]
        token_digests = [
            actor.get("token_sha256") for actor in actors if isinstance(actor, dict)
        ]
        if len(actor_ids) != len(set(actor_ids)):
            raise OperatorAuthorizationError(
                "operator authorization policy has a duplicate actor_id"
            )
        if len(token_digests) != len(set(token_digests)):
            raise OperatorAuthorizationError(
                "operator authorization policy has a duplicate token digest"
            )
    if policy.get("operations_enabled") is not True:
        raise OperatorAuthorizationError(
            "operator-authorized operations are disabled"
        )
    if not isinstance(policy.get("approval"), dict) or not isinstance(
        policy.get("actors"), list
    ):
        raise OperatorAuthorizationError(
            "operator authorization policy is not approved"
        )
    return policy


def token_from_header(authorization: str | None) -> str:
    if not isinstance(authorization, str) or not authorization.startswith(
        "Bearer "
    ):
        raise OperatorAuthorizationError(
            "authenticated operator bearer token is required"
        )
    token = authorization[7:]
    if (
        not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        raise OperatorAuthorizationError(
            "authenticated operator bearer token is invalid"
        )
    return token


def authenticate_token(
    token: str,
    action: str,
    *,
    claimed_display_name: str | None = None,
) -> dict:
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise OperatorAuthorizationError(
            "authenticated operator bearer token is invalid"
        )
    policy = _load_policy()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    matches = []
    for actor in policy["actors"]:
        if not isinstance(actor, dict):
            raise OperatorAuthorizationError(
                "operator authorization policy is invalid"
            )
        candidate = actor.get("token_sha256")
        if isinstance(candidate, str) and hmac.compare_digest(digest, candidate):
            matches.append(actor)
    if len(matches) != 1 or action not in matches[0].get("allowed_actions", []):
        raise OperatorAuthorizationError(
            "operator is not authorized for this action"
        )
    matched = matches[0]
    if (
        claimed_display_name is not None
        and claimed_display_name != matched.get("display_name")
    ):
        raise OperatorAuthorizationError(
            "operator display name does not match authenticated identity"
        )
    identity = {
        "actor_id": matched["actor_id"],
        "display_name": matched["display_name"],
        "role": matched["role"],
        "policy_version": policy["policy_version"],
        "authentication_method": "bearer_sha256_policy",
    }
    if "medical_actor_id" in matched:
        identity["medical_actor_id"] = matched["medical_actor_id"]
    return identity


def authenticate_environment(
    action: str,
    *,
    claimed_display_name: str | None = None,
) -> dict:
    token = os.environ.get(TOKEN_ENV, "")
    return authenticate_token(
        token,
        action,
        claimed_display_name=claimed_display_name,
    )
