"""The format-study contract is a governed live-pipeline artifact."""

import copy
import json
from pathlib import Path

import dao

from _validation import load_registry, schema_name_for, validate_instance


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_NAME = "loss_adjustment_report.schema.json"
EXAMPLE = (
    ROOT
    / "loss-adjustment-format-study"
    / "analysis"
    / "examples"
    / "example-disease-benefit.json"
)


def _example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _validate(document: dict) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(document, SCHEMA_NAME, schemas, registry)


def test_structured_report_schema_is_available_to_the_dao_registry():
    schemas, _ = load_registry()

    assert SCHEMA_NAME in schemas
    assert schema_name_for(Path("loss_adjustment_report_v1.json")) == SCHEMA_NAME


def test_dao_registry_runs_the_full_cross_field_validator():
    document = _example()
    document["sections"]["summary"]["statements"][0]["evidence_refs"] = ["E999"]

    errors = _validate(document)

    assert any("unknown evidence reference E999" in error for error in errors)


def test_dao_can_write_a_valid_structured_report(
    isolated_dao, make_args, tmp_path
):
    data_file = tmp_path / "loss-adjustment-report.json"
    data_file.write_text(json.dumps(_example()), encoding="utf-8")
    args = make_args(
        filename="loss_adjustment_report_v1.json",
        data_file=str(data_file),
        schema_name=SCHEMA_NAME,
        stage="draft_report_v1",
    )

    assert dao.cmd_write_contract(args) == 0

    written = (
        isolated_dao / "outputs" / "CASE_009" / "loss_adjustment_report_v1.json"
    )
    assert written.exists()


def test_dao_refuses_a_cross_field_invalid_structured_report(
    isolated_dao, make_args, tmp_path
):
    document = copy.deepcopy(_example())
    document["sections"]["summary"]["statements"][0]["evidence_refs"] = ["E999"]
    data_file = tmp_path / "invalid-loss-adjustment-report.json"
    data_file.write_text(json.dumps(document), encoding="utf-8")
    args = make_args(
        filename="loss_adjustment_report_v1.json",
        data_file=str(data_file),
        schema_name=SCHEMA_NAME,
        stage="draft_report_v1",
    )

    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_009" / "loss_adjustment_report_v1.json"
    ).exists()
