import json
import subprocess
from pathlib import Path


SCHEMA_PATH = Path(
    "loss-adjustment-format-study/analysis/loss-adjustment-report.schema.json"
)
RULEBOOK_PATH = Path(
    "loss-adjustment-format-study/analysis/llm-authoring-rules.md"
)


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_every_schema_family_and_mechanism_is_documented_in_rulebook():
    schema = _schema()
    rulebook = RULEBOOK_PATH.read_text(encoding="utf-8")
    profile = schema["$defs"]["documentProfile"]["properties"]

    for value in profile["family"]["enum"]:
        assert f"`{value}`" in rulebook
    for value in profile["claim_mechanism"]["enum"]:
        assert f"`{value}`" in rulebook


def test_family_mechanism_map_covers_the_schema_enums_exactly():
    schema = _schema()
    profile = schema["$defs"]["documentProfile"]["properties"]
    families = set(profile["family"]["enum"])
    mechanisms = set(profile["claim_mechanism"]["enum"])
    mapping = profile["claim_mechanism"]["x-family-mechanisms"]

    assert set(mapping) == families
    assert {value for values in mapping.values() for value in values} == mechanisms


def test_every_canonical_render_component_is_documented_in_rulebook():
    schema = _schema()
    rulebook = RULEBOOK_PATH.read_text(encoding="utf-8")
    components = schema["$defs"]["documentProfile"]["properties"][
        "ordered_components"
    ]["items"]["enum"]

    for component in components:
        assert f"`{component}`" in rulebook


def test_protected_study_source_root_is_git_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "sources/protected.pdf"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
