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
        config=config, documents=documents, medical_revision_context=None)
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


def test_claim_analysis_produces_a_valid_result_and_reads_cost_documents_only_for_dates() -> None:
    """A cost document answers date fields and nothing else.

    Until 2026-08-22 this asserted the receipt was never opened at all. That
    absolute rule cost real data: measured on CASE_705 against CASE_907 over
    the same source material, `surgery_or_procedure_date`, `imaging_date` and
    `treatment_period`'s end date exist ONLY on the 진료비 세부산정내역 pages --
    the clinical note says `Plan> admission, 내일 Op.` with no date -- so the
    selective lane published three fields as unavailable while the legacy lane
    had them. The block now means "never opened for an AMOUNT".
    """
    config = _config()
    result, _, read_documents, _ = _run_claim_analysis(config)

    assert _errors(result, "claim_analysis_result.schema.json") == []

    fields_using_cost_doc = {
        row["field_id"]
        for row in result["claim_facts"]
        for observation in row.get("observations") or []
        for reference in observation.get("evidence_references") or []
        if reference.get("document_id") == "DOC_004"
    }
    assert fields_using_cost_doc <= selection.COST_DOCUMENT_DATE_FIELDS, (
        "a cost document answered a field that is not a date field: "
        f"{sorted(fields_using_cost_doc - selection.COST_DOCUMENT_DATE_FIELDS)}"
    )


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
    assert fact["selected_observation_ids"] == []
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


def _agent_verdicts(items, *, outcome="confirmed"):
    """Stand in for the consistency-check agent.

    The agent's judgement is a reading of the documents, so an E2E cannot
    produce it; what it CAN check is that the seam holds -- that a verdict
    written against prepared items registers, and that the summary carries the
    values the helper will insist on.
    """
    verdicts = []
    for item in items:
        verdict = {
            "conflict_candidate_id": item["conflict_candidate_id"],
            "candidate_digest": item["candidate_digest"],
            "outcome": outcome,
            "reason": "the two records state different values",
        }
        if outcome == "confirmed":
            values = [row["value"] for row in item["readings"]
                      if row.get("value_state") == "asserted"]
            verdict["professional_summary"] = (
                "기록 간 값이 다릅니다: " + " / ".join(values)
                + ". 어느 기록이 사고 사실을 반영하는지 확인이 필요합니다."
            )
        verdicts.append(verdict)
    return verdicts


def test_consistency_check_prepares_judges_then_registers(monkeypatch) -> None:
    """The three-part seam: prepare -> agent verdict -> register.

    prepare must hand over the readings WITHOUT a verdict, and register must
    only act on what an agent decided -- so the same run also proves the helper
    registers nothing when no verdict exists.
    """
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)

    items = checker.build_work_items(result, config)
    assert len(items) == 1
    assert "outcome" not in items[0]
    assert items[0]["decision_bearing"] is True

    # No agent input: nothing is registered, and nothing can be.
    assert checker.register_confirmed(
        "CASE_9001", "RUN_20260819_1", "consistency-check", [], items) == {}

    verdicts = _agent_verdicts(items)
    assert checker.validate_verdicts(verdicts, items) == []

    ledger_calls: list[list[str]] = []
    monkeypatch.setattr(
        checker, "_dao_write",
        lambda args: (ledger_calls.append(args), "PASS: added CONFLICT_1")[1])
    registered = checker.register_confirmed(
        "CASE_9001", "RUN_20260819_1", "consistency-check", verdicts, items)

    assert registered
    assert ledger_calls[0][0] == "add-conflict-entry"
    # Raised under consistency_check, not claim_analysis.
    assert ledger_calls[0][ledger_calls[0].index("--stage") + 1] == "consistency_check"
    # The professional summary reaches the ledger with the entry.
    assert "--professional-summary" in ledger_calls[0]

    contract = checker.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=verdicts, registered=registered)
    assert _errors(contract, "evidence_validation_result.schema.json") == []


def test_screening_report_assembles_from_both_upstream_contracts() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    items = checker.build_work_items(result, config)
    verdicts = _agent_verdicts(items)
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

    assert _errors(report, "screening_report_selective.schema.json") == []
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
    assert outcome.status == "unavailable"
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
    fact["selected_observation_ids"] = ["CAO_9999"]
    errors = _errors(result, "claim_analysis_result.schema.json")
    assert any("not owned by the field" in error for error in errors), errors


def test_a_candidate_pointing_outside_its_field_is_refused() -> None:
    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    result["conflict_candidates"][0]["observation_ids"] = ["CAO_0001", "CAO_9999"]
    errors = _errors(result, "claim_analysis_result.schema.json")
    assert any("outside field" in error for error in errors), errors


def test_the_shipped_config_is_activated_with_a_recorded_approval() -> None:
    """The gate is OPEN in the shipped config, and says who opened it.

    Inverted 2026-08-20 when the legacy spine was deleted. The former assertion
    (`behavior_enabled is False`) guarded a rollback target that no longer
    exists, so leaving it would have failed forever while testing nothing. The
    flag survives the deletion deliberately -- `require_enabled` still refuses a
    config that turns it off -- so what needs guarding now is that the shipped
    config is the activated one AND that its approval block is present, since
    the schema is what forbids enabling without a recorded approver.
    """
    config = _config()
    assert config["behavior_enabled"] is True
    activation = config["activation"]
    for key in ("approved_by", "authority_role", "approved_at", "scope"):
        assert activation.get(key), f"activation is missing {key}"
    claim_driver.require_enabled(config)


def test_turning_the_flag_off_still_refuses_to_run() -> None:
    """The flag is retained, so its closed state must still be a hard stop.

    There is no legacy spine to fall back to any more: a disabled config must
    halt the stage, never silently run an unrouted read-everything pass.
    """
    config = _config()
    config["behavior_enabled"] = False
    with pytest.raises(RuntimeError, match="behavior_enabled=false"):
        claim_driver.require_enabled(config)


def test_the_legacy_driver_module_is_gone() -> None:
    """`run_claim_analysis.py` was deleted 2026-08-20; nothing may import it.

    Guards the deletion itself. The selective driver never imported the legacy
    one, and downstream reads only `claim_analysis_result.json`, so the module
    reappearing would mean a second writer of the stage had returned.
    """
    import importlib.util

    assert importlib.util.find_spec("run_claim_analysis") is None


def test_extracted_claim_fields_stays_the_medical_projection() -> None:
    """The legacy compatibility contract keeps its existing owner.

    `medical_repository` publishes `extracted_claim_fields.json` as the sealed
    deterministic projection of `medical_variables.json`. The selective result
    never becomes a second writer of it, and -- separately -- never claims that
    projection authority for itself: it publishes source-grounded readings.
    """
    import medical_repository

    assert "extracted_claim_fields.json" in medical_repository.MEDICAL_OWNED_CONTRACTS
    assert "claim_analysis_result.json" not in medical_repository.MEDICAL_OWNED_CONTRACTS

    config = _config()
    result, _, _, _ = _run_claim_analysis(config)
    assert result["medical_projection_status"] == "not_configured"
    assert "medical_revision" not in result
    for fact in result["claim_facts"]:
        expected = ("claim_analysis_native"
                    if fact["domain_code"] in claim_driver.NATIVE_DOMAINS
                    else "source_document_extraction")
        assert fact["authority"] == expected
