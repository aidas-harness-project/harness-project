"""Shared schema-validation core used by validate_output.py and dao.py.

Not a CLI itself -- import validate_instance()/schema_name_for() from here
rather than duplicating validation logic in two places.
"""
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
TEMPLATE_REGISTRY = ROOT / "templates" / "registry.json"


def load_registry():
    schemas = {}
    for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schemas[p.name] = json.loads(p.read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        (name, Resource.from_contents(s)) for name, s in schemas.items()
    )
    return schemas, registry


def schema_name_for(json_path: Path) -> str | None:
    """Derive the schema filename from a contract filename.

    e.g. critic_result_v2.json -> critic_result.schema.json
         draft_report_v1.evidence.json -> evidence_sidecar.schema.json
         normalized_policy_clause_DOC_004.json -> normalized_policy_clause.schema.json

    *.evidence.json is special-cased: Path.stem only strips one suffix, so
    for a name like "draft_report_v1.evidence.json" it yields
    "draft_report_v1.evidence" -- the _v\\d+ stripping below never reaches
    it, and every sidecar (whatever document it belongs to) maps to the
    same evidence_sidecar.schema.json regardless.

    _DOC_\\d+$ stripping is for policy-pipeline's one-file-per-policy-document
    output (normalized_policy_clause_{document_id}.json) -- without it this
    always returned None for those files, meaning validate_output.py would
    silently SKIP every one of them instead of validating. write-contract
    itself isn't affected (it takes --schema-name explicitly), but the
    standalone CLI tool's auto-derivation needs this too.

    Leading-underscore stripping is for the shared-state files
    (_source_ledger.json, _run_state.json, _conflict_ledger.json) -- their
    on-disk names carry a leading underscore (the project's convention for
    "internal/shared state, not a component's own output") but their
    schema files don't (source_ledger.schema.json, not
    _source_ledger.schema.json). Without this, all three always returned
    None -- found by actually running validate_output.py against a real
    forked case's ledger and getting SKIP instead of PASS.
    """
    if json_path.name.endswith(".evidence.json"):
        candidate = "evidence_sidecar.schema.json"
        return candidate if (SCHEMA_DIR / candidate).exists() else None
    stem = json_path.stem
    stem = stem.lstrip("_")
    stem = re.sub(r"_v\d+$", "", stem)
    stem = re.sub(r"_CASE_\d+$", "", stem)
    stem = re.sub(r"_DOC_\d+$", "", stem)
    candidate = f"{stem}.schema.json"
    return candidate if (SCHEMA_DIR / candidate).exists() else None


def validate_instance(instance: dict, schema_name: str, schemas: dict, registry) -> list[str]:
    """Return a list of human-readable error strings; empty means PASS.

    format_checker is required, not decorative: date_field's whole contract
    is `format: date`, and without a FormatChecker jsonschema silently skips
    every `format` keyword -- CASE_021's run surfaced that a malformed date
    would have validated. (date-time additionally needs rfc3339-validator
    installed to be checked; date is built in.)"""
    validator = Draft202012Validator(schemas[schema_name], registry=registry,
                                     format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors:
        loc = "/".join(map(str, e.absolute_path)) or "(root)"
        out.append(f"{loc}: {e.message}")
    if schema_name == "case_type_result.schema.json":
        out.extend(_report_profile_errors(instance))
    return out


def _report_profile_errors(instance: dict) -> list[str]:
    """Validate the case-type profile against the renderer registry.

    JSON Schema validates the profile internally.  This small cross-file check
    establishes that its selected template carries the same family, mechanism,
    mode, and support status.  Historical results without a profile keep their
    previous validation behavior.
    """
    profile = instance.get("report_profile")
    if not isinstance(profile, dict):
        return []
    template_id = instance.get("template_id")
    if profile.get("support_status") == "unsupported":
        return [] if template_id is None else [
            "template_id: unsupported report_profile must not select a template"
        ]
    templates = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))["templates"]
    template = templates.get(template_id)
    if template is None:
        return [f"template_id: {template_id!r} is not registered"]
    matches = (
        profile.get("family") in template.get("report_families", [])
        and profile.get("claim_mechanism") in template.get("claim_mechanisms", [])
        and profile.get("mode") == template.get("mode")
        and profile.get("support_status") == template.get("support_status")
    )
    return [] if matches else [
        f"template_id: {template_id!r} does not match report_profile"
    ]
