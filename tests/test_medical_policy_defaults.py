"""Safety defaults for reconstructed medical policy artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from _validation import load_registry, validate_instance


CONFIGS = {
    "medical_projection_v0.1.json": (
        "projection_enabled",
        "medical_projection_config.schema.json",
    ),
    "medical_referral_policy_v0.1.json": (
        "policy_enabled",
        "medical_referral_policy.schema.json",
    ),
    "medical_review_request_v0.1.json": (
        "requests_enabled",
        "medical_review_request_config.schema.json",
    ),
    "medical_review_roles_v0.1.json": (
        "operations_enabled",
        "medical_review_role_config.schema.json",
    ),
    "medical_structuring_v0.1.json": (
        "behavior_enabled",
        "medical_structuring_config.schema.json",
    ),
}


def test_all_shipped_medical_v0_1_policies_are_valid_and_disabled() -> None:
    schemas, registry = load_registry()
    config_dir = ROOT / "config" / "medical"

    assert not list(config_dir.glob("*_v0.2.json"))
    for file_name, (enable_field, schema_name) in CONFIGS.items():
        payload = json.loads((config_dir / file_name).read_text(encoding="utf-8"))
        assert payload[enable_field] is False, file_name
        assert payload["approval"] is None, file_name
        assert validate_instance(payload, schema_name, schemas, registry) == []
