"""Schema tests for the fail-closed medical-review bootstrap."""
import json
from pathlib import Path

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent


def errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def test_disabled_medical_review_role_bootstrap_validates():
    config = json.loads((ROOT / "config/medical/medical_review_roles_v0.1.json").read_text(encoding="utf-8"))
    assert not errors(config, "medical_review_role_config.schema.json")
    assert config["operations_enabled"] is False
    assert config["named_actors"] == []
    assert config["specialty_codes"] == ["unspecified"]


def test_disabled_medical_request_bootstrap_validates():
    config = json.loads((ROOT / "config/medical/medical_review_request_v0.1.json").read_text(encoding="utf-8"))
    assert not errors(config, "medical_review_request_config.schema.json")
    assert config["requests_enabled"] is False
    assert config["interpretations"] == []


def test_disabled_referral_policy_bootstrap_validates():
    config = json.loads((ROOT / "config/medical/medical_referral_policy_v0.1.json").read_text(encoding="utf-8"))
    assert not errors(config, "medical_referral_policy.schema.json")
    assert config["policy_enabled"] is False
    assert config["severity_definitions"] == []
    assert config["referral_rules"] == []


def test_enabling_review_roles_without_approval_and_named_actors_fails():
    config = json.loads((ROOT / "config/medical/medical_review_roles_v0.1.json").read_text(encoding="utf-8"))
    config["operations_enabled"] = True
    messages = errors(config, "medical_review_role_config.schema.json")
    assert any("approval" in message or "named_actors" in message for message in messages)