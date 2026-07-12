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
    """
    if json_path.name.endswith(".evidence.json"):
        candidate = "evidence_sidecar.schema.json"
        return candidate if (SCHEMA_DIR / candidate).exists() else None
    stem = json_path.stem
    stem = re.sub(r"_v\d+$", "", stem)
    stem = re.sub(r"_CASE_\d+$", "", stem)
    stem = re.sub(r"_DOC_\d+$", "", stem)
    candidate = f"{stem}.schema.json"
    return candidate if (SCHEMA_DIR / candidate).exists() else None


def validate_instance(instance: dict, schema_name: str, schemas: dict, registry) -> list[str]:
    """Return a list of human-readable error strings; empty means PASS."""
    validator = Draft202012Validator(schemas[schema_name], registry=registry)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors:
        loc = "/".join(map(str, e.absolute_path)) or "(root)"
        out.append(f"{loc}: {e.message}")
    return out
