"""Synthetic end-to-end: Stage 2 classification -> Claim Analysis ->
Consistency Check -> Screening Report.

One fabricated case, built in memory from the version-controlled config and
schemas. It never touches source-cases, outputs, data, or ground truth; DAO
writes run against the `isolated_dao` tmp_path fixture.

The point of this file is the SEAMS: that each stage's real output is the next
stage's real input, and that the four fail-closed conditions actually refuse.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import claim_analysis_selection as selection
import dao
import medical_document_routing as routing
import run_claim_analysis_selective as claim_driver
import run_consistency_check as checker
import run_screening_report as reporter


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)
MEDICAL_REVISION = {
    "sha256": "c" * 64,
    "run_id": "RUN_20260819_1",
    "schema_version": "medical_variables.v0.1",
    "config_version": "medical_structuring.v0.1",
}

# One synthetic case: a fall at work, treated surgically, with a diagnosis
# certificate and a discharge summary that disagree on laterality.
PAGES = {
    ("DOC_001", 1): (
        "진 단 서\n"
        "환자는 2026-03-02 작업 중 사다리에서 추락하여 내원함.\n"
        "진단명: 우측 요골 골절\n"
        "진료과: 정형외과\n"
    ),
    ("DOC_002", 1): (
        "입퇴원요약\n"
        "진단명: 좌측 요골 골절\n"
        "수술: 관혈적 정복술 시행\n"
    ),
    ("DOC_003", 1): (
        "수 술 기 록\n"
        "수술명: 관혈적 정복 및 금속판 내고정술\n"
        "수술일: 2026-03-03\n"
    ),
    ("DOC_004", 1): (
        "진료비 계산서 영수증\n총액 1,240,000원\n"
    ),
}

MANIFEST = {"documents": [
    {"document_id": "DOC_001", "medical_classification": {
        "status": "deterministic_title", "kind": "diagnosis_certificate"}},
    {"document_id": "DOC_002", "medical_classification": {
        "status": "deterministic_title", "kind": "admission_discharge_summary"}},
    {"document_id": "DOC_003", "medical_classification": {
        "status": "deterministic_title", "kind": "surgery_procedure_record"}},
    {"document_id": "DOC_004", "medical_classification": {
        "status": "deterministic_title", "kind": "medical_expense_receipt"}},
]}


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _extractor(pages):
    """A stand-in for the model call, answering from the synthetic pages.

    Deliberately literal: it only ever returns a quote that is verbatim in the
    page it names, so the driver's own verification is exercised rather than
    bypassed.
    """
    answers = {
        ("primary_diagnosis", "DOC_001"): ("우측 요골 골절", 1),
        ("primary_diagnosis", "DOC_002"): ("좌측 요골 골절", 1),
        ("diagnosis_department", "DOC_001"): ("정형외과", 1),
        ("surgery_or_procedure_date", "DOC_003"): ("2026-03-03", 1),
        ("accident_date", "DOC_001"): ("2026-03-02", 1),
        ("work_activity_context", "DOC_001"): ("work_activity", 1),
        ("injury_event_present", "DOC_001"): ("사다리에서 추락", 1),
    }
    read_documents: set[str] = set()

    def extract(field_row, document_id, kind, rank):
        read_documents.add(document_id)
        found = answers.get((field_row["field_id"], document_id))
        if found is None:
            return None
        quote, page = found
        value = quote
        if field_row["field_id"] == "work_activity_context":
            value = "work_activity"
            quote = "작업 중"
        if field_row["field_id"] == "injury_event_present":
            value = True
            quote = "사다리에서 추락"
        return {"value": value, "page": page, "quote": quote}

    return extract, read_documents


def _run_claim_analysis(config):
    documents = claim_driver.classified_documents(MANIFEST)
    extract, read_documents = _extractor(PAGES)
    ids = claim_driver._observation_id_sequence()
    outcomes = []
    for wave in selection.SEARCHING_WAVES:
        for plan in selection.plan_wave(config, documents, wave):
            field_row = next(
                row for row in config["fields"] if row["field_id"] == plan.field_id
            )
            outcomes.append(claim_driver.resolve_field(
                plan, field_row, config, extract, PAGES, ids))
    result = claim_driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config, documents=documents, medical_revision=MEDICAL_REVISION)
    return result, outcomes, read_documents, documents


# ------------------------------------------------------------------ flow --

def test_stage2_classification_feeds_the_planner() -> None:
    documents = claim_driver.classified_documents(MANIFEST)
    kinds = {ref.document_id: ref.kind for ref in documents}
    assert kinds == {
        "DOC_001": "diagnosis_certificate",
        "DOC_002": "admission_discharge_summary",
        "DOC_003": "surgery_procedure_record",
        "DOC_004": "medical_expense_receipt",
    }
    # And the titles those classifications came from resolve without an LLM.
    assert routing.medical_kind_from_title("진 단 서") == "diagnosis_certificate"
    assert routing.medical_kind_from_title("수 술 기 록") == "surgery_procedure_record"


def test_claim_analysis_produces_a_valid_result_and_never_reads_cost_documents() -> None:
    config = _config()
    result, _, read_documents, _ = _run_claim_analysis(config)

    assert _errors(result, "claim_analysis_result.schema.json") == []
    # The receipt is required-document evidence, never opened for content.
    assert "DOC_004" not in read_documents


def test_a_real_disagreement_survives_as_a_candidate_with_both_readings() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)

    candidates = result["conflict_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["field_id"] == "primary_diagnosis"

    fact = next(
        row for row in result["claim_facts"]
        if row["field_id"] == "primary_diagnosis"
    )
    assert fact["resolution_status"] == "conflict"
    assert fact["canonical_observation_ids"] == []
    values = {row["value"] for row in fact["observations"]}
    assert values == {"우측 요골 골절", "좌측 요골 골절"}


def test_claim_analysis_writes_no_conflict_ledger_entry(monkeypatch) -> None:
    """The stage is conflict-gated, so it must not raise its own P6 entry."""
    config = _config()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        claim_driver, "_dao_write", lambda args: calls.append(args))
    result, outcomes, _, documents = _run_claim_analysis(config)
    trace = claim_driver.build_trace(
        case_id="CASE_9001", run_id="RUN_20260819_1", config=config,
        outcomes=outcomes, document_dispositions=[], provider_calls=0)
    claim_driver.publish(
        case_id="CASE_9001", run_id="RUN_20260819_1", held_by="claim-analysis",
        result=result, trace=trace)

    assert calls, "the driver must publish its contracts"
    for args in calls:
        assert args[0] == "write-contract"
        assert "add-conflict-entry" not in args
        assert "set-conflict-verdict" not in args
    assert "claim_analysis" in {
        args[args.index("--stage") + 1] for args in calls if "--stage" in args
    }


def test_consistency_check_confirms_the_material_conflict(monkeypatch) -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)

    verdicts = checker.verify_candidates(result, config)
    assert [row["outcome"] for row in verdicts] == ["confirmed"]

    ledger_calls: list[list[str]] = []
    monkeypatch.setattr(
        checker, "_dao_write",
        lambda args: (ledger_calls.append(args), "PASS: added CONFLICT_1")[1])
    registered = checker.register_confirmed(
        "CASE_9001", "RUN_20260819_1", "consistency-check", verdicts)

    assert registered
    assert ledger_calls[0][0] == "add-conflict-entry"
    # Raised under consistency_check, not claim_analysis.
    assert ledger_calls[0][ledger_calls[0].index("--stage") + 1] == "consistency_check"

    contract = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=verdicts, registered=registered)
    assert _errors(contract, "evidence_validation_result.schema.json") == []


def test_screening_report_assembles_from_both_upstream_contracts() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    verdicts = checker.verify_candidates(result, config)
    registered = {verdicts[0]["conflict_candidate_id"]: "CONFLICT_1"}
    consistency = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=verdicts, registered=registered)

    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=result, consistency=consistency, config=config,
        conflict_entries={"CONFLICT_1": {
            "field_or_topic": "primary_diagnosis",
            "sources": [
                {"document_id": "DOC_001", "value": "우측 요골 골절", "quote": "우측 요골 골절"},
                {"document_id": "DOC_002", "value": "좌측 요골 골절", "quote": "좌측 요골 골절"},
            ]}})

    assert _errors(report, "screening_report.schema.json") == []
    # All four verdicts present.
    assert len(report["case_summary"]["case_type_assessment"]) == 4
    by_type = {
        row["case_type"]: row
        for row in report["case_summary"]["case_type_assessment"]
    }
    # Work activity was documented, so 산재 is supported; a car was never
    # mentioned, so 교통사고 stays uncertain rather than becoming 비해당.
    assert by_type["industrial_accident"]["status_label"] == "해당"
    assert by_type["traffic_accident"]["status_label"] == "불확실"
    # The confirmed conflict is carried, bound by id.
    assert report["inconsistencies"][0]["conflict_ref"] == "CONFLICT_1"
    # And no development detail leaked in.
    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in ("stop_reason", "provider_calls", "conflict_candidate"):
        assert forbidden not in serialized


# ---------------------------------------------------------- fail closed --

def test_a_fabricated_quote_is_dropped_before_it_can_be_published() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(
        field_row, config,
        [selection.DocumentRef("DOC_001", "diagnosis_certificate")])

    def lying_extract(field_row, document_id, kind, rank):
        return {"value": "존재하지 않는 진단", "page": 1,
                "quote": "이 문장은 페이지에 없습니다"}

    outcome = claim_driver.resolve_field(
        plan, field_row, config, lying_extract, PAGES,
        claim_driver._observation_id_sequence())
    assert outcome.status == "unknown"
    assert outcome.observations == []


def test_dao_refuses_a_result_whose_exact_range_does_not_match(
    isolated_dao, make_args, monkeypatch, tmp_path
) -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    # Corrupt one offset so the cited range no longer spells the quote.
    fact = next(
        row for row in result["claim_facts"]
        if row["field_id"] == "primary_diagnosis"
    )
    fact["observations"][0]["evidence_references"][0]["start_char"] += 3

    data_file = tmp_path / "result.json"
    data_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dao, "_load_medical_revision", lambda case_id, sha: ({
        "case_id": case_id, "run_id": "RUN_20260819_1",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
    }, None))
    monkeypatch.setattr(
        dao, "read_redacted_text_bundle_data",
        lambda case_id, doc_ids: {"case_id": case_id, "documents": [
            {"document_id": doc_id,
             "pages": [{"page": 1, "text": PAGES[(doc_id, 1)]}]}
            for doc_id in doc_ids
        ]})

    args = make_args(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        filename="claim_analysis_result.json", data_file=str(data_file),
        schema_name="claim_analysis_result.schema.json")
    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_9001" / "claim_analysis_result.json"
    ).exists()


def test_dangling_observation_id_is_refused_by_the_semantic_validator() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    fact = next(
        row for row in result["claim_facts"]
        if row["field_id"] == "surgery_or_procedure_date"
    )
    if fact["resolution_status"] != "resolved":
        pytest.skip("fixture did not resolve this field")
    fact["canonical_observation_ids"] = ["CAO_9999"]
    errors = _errors(result, "claim_analysis_result.schema.json")
    assert any("not owned by the field" in error for error in errors), errors


def test_a_candidate_pointing_outside_its_field_is_refused() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    result["conflict_candidates"][0]["observation_ids"] = ["CAO_0001", "CAO_9999"]
    errors = _errors(result, "claim_analysis_result.schema.json")
    assert any("outside field" in error for error in errors), errors


def test_the_whole_flow_stays_disabled_until_activation_is_recorded() -> None:
    config = _config()
    assert config["behavior_enabled"] is False
    with pytest.raises(RuntimeError, match="behavior_enabled=false"):
        claim_driver.require_enabled(config)


def test_legacy_driver_keeps_its_own_path_while_the_gate_is_closed() -> None:
    """Backward compatibility: with the gate closed nothing reroutes."""
    import run_claim_analysis as legacy

    assert legacy.selective_routing_enabled() is False
    # The four legacy contracts are still the ones the legacy spine writes.
    assert legacy.CONTRACT == "extracted_claim_fields.json"
    assert legacy.CONTRACT_CP3 == "case_type_result.json"


def test_enabling_the_gate_reroutes_the_legacy_entry_point(monkeypatch, capsys) -> None:
    import run_claim_analysis as legacy

    monkeypatch.setattr(legacy, "selective_routing_enabled", lambda: True)
    monkeypatch.setattr(
        legacy, "run",
        lambda **kwargs: pytest.fail("the legacy spine must not run when enabled"))
    rc = legacy.main([
        "CASE_9001", "--held-by", "claim-analysis", "--run-id", "RUN_20260819_1",
    ])
    assert rc == 2
    assert "run_claim_analysis_selective.py" in capsys.readouterr().err


def test_extracted_claim_fields_stays_the_medical_projection() -> None:
    """The legacy compatibility contract keeps its existing owner.

    `medical_repository` publishes `extracted_claim_fields.json` as the sealed
    deterministic projection of `medical_variables.json`. The selective result
    binds to the same revision rather than becoming a second writer of it.
    """
    import medical_repository

    assert "extracted_claim_fields.json" in medical_repository.MEDICAL_OWNED_CONTRACTS
    assert "claim_analysis_result.json" not in medical_repository.MEDICAL_OWNED_CONTRACTS

    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    assert result["medical_revision"]["sha256"] == MEDICAL_REVISION["sha256"]
    for fact in result["claim_facts"]:
        if fact["domain_code"] in claim_driver.MEDICAL_DOMAINS:
            assert fact["authority"] == "medical_variables_projection"
