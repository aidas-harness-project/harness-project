"""Consistency Check driver tests.

Synthetic only: version-controlled config/schemas and in-memory contracts.
Never reads source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import run_consistency_check as checker


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _observation(observation_id: str, value, *, state="asserted", evidence=True) -> dict:
    row = {
        "observation_id": observation_id,
        "value_state": state,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": f"q-{observation_id}",
            "start_char": 0, "end_char": 3,
        }] if evidence else [],
    }
    if state == "asserted":
        row.update({
            "value": value,
            "source_document_kind": "diagnosis_certificate",
            "source_priority_rank": 1,
        })
    else:
        row["reason"] = "not stated"
    return row


def _result_with(field_id: str, observations: list[dict]) -> dict:
    return {
        "claim_facts": [{
            "field_id": field_id,
            "domain_code": "diagnosis",
            "priority_grade": "A",
            "authority": "source_document_extraction",
            "resolution_status": "conflict",
            "selected_observation_ids": [],
            "observations": observations,
            "conflict_candidate_ids": ["CAC_0001"],
            "stop_reason": "conflict_found",
            "resolution_reason": "sources disagree",
        }],
        "conflict_candidates": [{
            "conflict_candidate_id": "CAC_0001",
            "field_id": field_id,
            "observation_ids": [row["observation_id"] for row in observations],
            "reason": "two sources disagree",
            "consistency_status": "pending_consistency_check",
        }],
    }


# ------------------------------------------------------------ materiality --

def test_critical_field_set_comes_from_the_routing_config() -> None:
    critical = checker.critical_field_ids(_config())
    # The decision-changing facts the brief names.
    for field_id in (
        "accident_date", "accident_mechanism", "primary_diagnosis",
        "diagnosis_site", "diagnosis_laterality", "surgery_or_procedure_name",
        "surgery_or_procedure_date", "disability_type",
    ):
        assert field_id in critical, field_id


def test_a_real_disagreement_on_a_critical_field_is_confirmed() -> None:
    result = _result_with("primary_diagnosis", [
        _observation("CAO_0001", "우측 요골 골절"),
        _observation("CAO_0002", "좌측 요골 골절"),
    ])
    verdicts = checker.verify_candidates(result, _config())
    assert [row["outcome"] for row in verdicts] == ["confirmed"]
    assert len(verdicts[0]["sources"]) == 2
    assert {row["value"] for row in verdicts[0]["sources"]} == {
        "우측 요골 골절", "좌측 요골 골절"
    }


def test_a_disagreement_on_a_non_critical_field_is_not_material() -> None:
    config = _config()
    non_critical = next(
        row["field_id"] for row in config["fields"]
        if not row["critical_conflict_field"]
    )
    result = _result_with(non_critical, [
        _observation("CAO_0001", "A"),
        _observation("CAO_0002", "B"),
    ])
    verdicts = checker.verify_candidates(result, config)
    assert verdicts[0]["outcome"] == "not_material"
    assert verdicts[0]["sources"] == []


# ------------------------------------------------------------ withdrawal --

def test_silence_on_one_side_is_withdrawn_not_a_conflict() -> None:
    result = _result_with("primary_diagnosis", [
        _observation("CAO_0001", "우측 요골 골절"),
        _observation("CAO_0002", None, state="unknown"),
    ])
    verdicts = checker.verify_candidates(result, _config())
    assert verdicts[0]["outcome"] == "withdrawn"
    assert "missing mention" in verdicts[0]["reason"]


def test_identical_values_are_withdrawn() -> None:
    result = _result_with("primary_diagnosis", [
        _observation("CAO_0001", "우측 요골 골절"),
        _observation("CAO_0002", "우측 요골 골절"),
    ])
    verdicts = checker.verify_candidates(result, _config())
    assert verdicts[0]["outcome"] == "withdrawn"


def test_a_reading_without_exact_evidence_cannot_establish_a_conflict() -> None:
    result = _result_with("primary_diagnosis", [
        _observation("CAO_0001", "우측 요골 골절"),
        _observation("CAO_0002", "좌측 요골 골절", evidence=False),
    ])
    verdicts = checker.verify_candidates(result, _config())
    assert verdicts[0]["outcome"] == "withdrawn"


def test_a_candidate_naming_a_missing_observation_is_withdrawn() -> None:
    result = _result_with("primary_diagnosis", [
        _observation("CAO_0001", "우측 요골 골절"),
        _observation("CAO_0002", "좌측 요골 골절"),
    ])
    result["conflict_candidates"][0]["observation_ids"] = ["CAO_0001", "CAO_9999"]
    verdicts = checker.verify_candidates(result, _config())
    assert verdicts[0]["outcome"] == "withdrawn"


# --------------------------------------------------------------- ledger --

def test_only_confirmed_candidates_reach_the_ledger(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_write(args):
        calls.append(args)
        return "PASS: added CONFLICT_1"

    monkeypatch.setattr(checker, "_dao_write", fake_write)
    verdicts = [
        {"conflict_candidate_id": "CAC_0001", "field_id": "primary_diagnosis",
         "outcome": "confirmed", "reason": "r",
         "sources": [
             {"document_id": "DOC_001", "page": 1, "value": "A", "quote": "qa"},
             {"document_id": "DOC_002", "page": 2, "value": "B", "quote": "qb"},
         ]},
        {"conflict_candidate_id": "CAC_0002", "field_id": "diagnosis_department",
         "outcome": "not_material", "reason": "r", "sources": []},
        {"conflict_candidate_id": "CAC_0003", "field_id": "primary_diagnosis",
         "outcome": "withdrawn", "reason": "r", "sources": []},
    ]
    registered = checker.register_confirmed(
        "CASE_9001", "RUN_20260819_1", "consistency-check", verdicts)

    assert registered == {"CAC_0001": "CONFLICT_1"}
    assert len(calls) == 1
    assert calls[0][0] == "add-conflict-entry"
    assert "--stage" in calls[0]
    assert calls[0][calls[0].index("--stage") + 1] == "consistency_check"


def test_ledger_entries_are_created_pending_and_never_auto_deferred(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        checker, "_dao_write",
        lambda args: (calls.append(args), "PASS: added CONFLICT_1")[1])
    checker.register_confirmed("CASE_9001", "RUN_20260819_1", "consistency-check", [
        {"conflict_candidate_id": "CAC_0001", "field_id": "primary_diagnosis",
         "outcome": "confirmed", "reason": "r",
         "sources": [
             {"document_id": "DOC_001", "page": 1, "value": "A", "quote": "qa"},
             {"document_id": "DOC_002", "page": 2, "value": "B", "quote": "qb"},
         ]},
    ])
    flat = " ".join(calls[0])
    # The driver must never set a verdict itself -- P6 leaves all three
    # dispositions to a human.
    assert "set-conflict-verdict" not in flat
    assert "deferred_to_report" not in flat
    assert "resolved" not in flat


def test_every_ledger_call_carries_a_fresh_operation_id(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        checker, "_dao_write",
        lambda args: (calls.append(args), "PASS: added CONFLICT_1")[1])
    verdicts = [
        {"conflict_candidate_id": f"CAC_000{n}", "field_id": "primary_diagnosis",
         "outcome": "confirmed", "reason": "r",
         "sources": [
             {"document_id": "DOC_001", "page": 1, "value": "A", "quote": "qa"},
             {"document_id": "DOC_002", "page": 2, "value": "B", "quote": "qb"},
         ]}
        for n in (1, 2)
    ]
    checker.register_confirmed("CASE_9001", "RUN_20260819_1", "cc", verdicts)
    ids = [args[args.index("--operation-id") + 1] for args in calls]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_a_ledger_call_that_reports_no_id_fails_loud(monkeypatch) -> None:
    monkeypatch.setattr(checker, "_dao_write", lambda args: "PASS: nothing useful")
    with pytest.raises(RuntimeError, match="did not report a conflict id"):
        checker.register_confirmed("CASE_9001", "RUN_20260819_1", "cc", [
            {"conflict_candidate_id": "CAC_0001", "field_id": "primary_diagnosis",
             "outcome": "confirmed", "reason": "r",
             "sources": [
                 {"document_id": "DOC_001", "page": 1, "value": "A", "quote": "qa"},
                 {"document_id": "DOC_002", "page": 2, "value": "B", "quote": "qb"},
             ]},
        ])


# ------------------------------------------------------------- contract --

def test_contract_validates_and_binds_conflict_ids_only_to_inconsistent() -> None:
    verdicts = [
        {"conflict_candidate_id": "CAC_0001", "field_id": "primary_diagnosis",
         "outcome": "confirmed", "reason": "real disagreement",
         "sources": [
             {"document_id": "DOC_001", "page": 1, "value": "A", "quote": "qa"},
             {"document_id": "DOC_002", "page": 2, "value": "B", "quote": "qb"},
         ]},
        {"conflict_candidate_id": "CAC_0002", "field_id": "primary_diagnosis",
         "outcome": "withdrawn", "reason": "one side is silent", "sources": []},
    ]
    contract = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1", verdicts=verdicts,
        registered={"CAC_0001": "CONFLICT_1"})

    assert _errors(contract, "evidence_validation_result.schema.json") == []
    by_result = {row["result"]: row for row in contract["checks"]}
    assert by_result["inconsistent"]["conflict_id"] == "CONFLICT_1"
    assert by_result["consistent"]["conflict_id"] is None


def test_contract_warns_when_a_confirmed_conflict_has_no_ledger_entry() -> None:
    contract = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=[{
            "conflict_candidate_id": "CAC_0001", "field_id": "primary_diagnosis",
            "outcome": "confirmed", "reason": "r", "sources": [],
        }],
        registered={})
    assert contract["warnings"]
    assert "CAC_0001" in contract["warnings"][0]


def test_no_candidates_produces_a_clean_empty_audit_record() -> None:
    contract = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1", verdicts=[], registered={})
    assert _errors(contract, "evidence_validation_result.schema.json") == []
    assert contract["checks"] == []
    assert contract["review_required"] is False
