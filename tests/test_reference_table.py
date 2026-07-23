"""Part 6: structured policy appendices with cell-level provenance."""
import json

import dao
from _cross_contract import check_reference_table


TEXT = (
    "<<<PAGE page=1>>>\n"
    "[별표] 장해분류표\n"
    "장해분류 지급률\n"
    "눈의 장해 50%\n"
    "<<<PAGE page=2>>>\n"
    "팔의 장해 30%\n"
)


def _contract():
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "tables": [{
            "table_uid": "RT-1111111111111111",
            "table_id": "T-1",
            "title": "장해분류표",
            "columns": [
                {"column_key": "classification", "label": "장해분류"},
                {"column_key": "rate", "label": "지급률"},
            ],
            "rows": [{
                "row_uid": "RR-1111111111111111",
                "cells": [
                    {
                        "cell_uid": "RC-1111111111111111",
                        "column_key": "classification",
                        "value": "눈의 장해",
                        "evidence_references": [{
                            "document_id": "DOC_001",
                            "page": 1,
                            "quote": "눈의 장해",
                        }],
                        "review_required": False,
                    },
                    {
                        "cell_uid": "RC-2222222222222222",
                        "column_key": "rate",
                        "value": "50%",
                        "evidence_references": [{
                            "document_id": "DOC_001",
                            "page": 1,
                            "quote": "50%",
                        }],
                        "review_required": False,
                    },
                ],
            }],
            "evidence_references": [{
                "document_id": "DOC_001",
                "page": 1,
                "quote": "장해분류표",
            }],
            "review_required": False,
        }],
    }


def _seed_source_and_manifest(isolated_dao):
    processed = (
        isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001")
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(TEXT, encoding="utf-8")
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    manifest = {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_030/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
        }],
    }
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_valid_reference_table_passes():
    assert check_reference_table(
        _contract(), "reference_table_DOC_001.json", TEXT) == []


def test_off_page_cell_is_rejected():
    contract = _contract()
    contract["tables"][0]["rows"][0]["cells"][0][
        "evidence_references"][0]["page"] = 2
    errors = check_reference_table(
        contract, "reference_table_DOC_001.json", TEXT)
    assert any("quote not found on page 2" in error for error in errors)


def test_foreign_document_cell_is_rejected():
    contract = _contract()
    contract["tables"][0]["rows"][0]["cells"][0][
        "evidence_references"][0]["document_id"] = "DOC_002"
    errors = check_reference_table(
        contract, "reference_table_DOC_001.json", TEXT)
    assert any("expected 'DOC_001'" in error for error in errors)


def test_cell_quote_must_contain_its_value():
    contract = _contract()
    cell = contract["tables"][0]["rows"][0]["cells"][0]
    cell["evidence_references"][0]["quote"] = "장해분류"
    errors = check_reference_table(
        contract, "reference_table_DOC_001.json", TEXT)
    assert any("does not contain" in error for error in errors)


def test_each_row_must_cover_declared_columns_once():
    contract = _contract()
    contract["tables"][0]["rows"][0]["cells"].pop()
    errors = check_reference_table(
        contract, "reference_table_DOC_001.json", TEXT)
    assert any("cover every declared column" in error for error in errors)


def test_reference_table_schema_accepts_valid_contract():
    assert dao._schema_check(
        _contract(), "reference_table.schema.json") == []


def test_dao_write_runs_reference_table_cross_contract(
        isolated_dao, make_args):
    _seed_source_and_manifest(isolated_dao)
    contract = _contract()
    contract["tables"][0]["rows"][0]["cells"][0][
        "evidence_references"][0]["quote"] = "존재하지 않는 셀"
    data_file = isolated_dao / "reference.json"
    data_file.write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    args = make_args(
        case_id="CASE_030",
        filename="reference_table_DOC_001.json",
        data_file=str(data_file),
        schema_name="reference_table.schema.json",
        held_by="policy-pipeline",
        run_id="RUN_20260724_001",
        stage="policy_clause_processing",
    )
    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_030" /
        "reference_table_DOC_001.json").exists()


def test_dao_write_persists_valid_reference_table(
        isolated_dao, make_args):
    _seed_source_and_manifest(isolated_dao)
    data_file = isolated_dao / "reference.json"
    data_file.write_text(
        json.dumps(_contract(), ensure_ascii=False), encoding="utf-8")
    args = make_args(
        case_id="CASE_030",
        filename="reference_table_DOC_001.json",
        data_file=str(data_file),
        schema_name="reference_table.schema.json",
        held_by="policy-pipeline",
        run_id="RUN_20260724_001",
        stage="policy_clause_processing",
    )
    assert dao.cmd_write_contract(args) == 0


def test_clause_link_resolves_table_and_rows_by_stable_uid(isolated_dao):
    _seed_source_and_manifest(isolated_dao)
    table_path = (
        isolated_dao / "outputs" / "CASE_030" /
        "reference_table_DOC_001.json")
    table_path.write_text(
        json.dumps(_contract(), ensure_ascii=False), encoding="utf-8")
    normalized = {"clauses": [{
        "reference_table_refs": [{
            "document_id": "DOC_001",
            "table_uid": "RT-1111111111111111",
            "row_uids": ["RR-1111111111111111"],
        }],
    }]}
    assert dao._reference_table_link_errors(
        "CASE_030", normalized) == []
    normalized["clauses"][0]["reference_table_refs"][0]["row_uids"] = [
        "RR-9999999999999999"]
    errors = dao._reference_table_link_errors(
        "CASE_030", normalized)
    assert any("row_uids do not resolve" in error for error in errors)
