"""V3 per-unit resume boundary for the claim-analysis and denial-response pilots.

These are DAO integration tests, not fakes of the DAO: every candidate read and
write goes through the real `dao.py` command functions against an isolated
outputs/data tree, so the schema validation, identity check, locking and
path-containment rules that guard `_driver_receipts/` are actually exercised.

The interruption mechanism is the explicit, test-owned seam in
`driver_runtime.interrupt_at`. Token exhaustion or killing the process are not
used: neither can name WHICH unit stops, and both leave the test unable to say
which units had genuinely completed -- which is the entire property under test.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import dao
import driver_runtime
import run_denial_response_driver as denial_driver


CASE = "CASE_009"
RUN = "RUN_20260813_1"
DOCS = ["DOC_001", "DOC_002", "DOC_003"]


def _page_text(doc_id: str) -> str:
    return f"<<<PAGE page=1>>>\nFinding for {doc_id}.\n"


def _dao_router(isolated_dao, make_args, capsys, *, manifest, stage):
    """Route a driver's DAO calls to the real dao.py command functions."""
    def read(args, *, allow_missing=False):
        command = args[0]
        if command == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": stage, "status": "in_progress"}]}
        if command == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if command == "read-contract":
            return None
        if command == "read-driver-receipt":
            return None
        if command == "read-driver-candidates":
            code = dao.cmd_read_driver_candidates(make_args(
                case_id=args[1], stage=args[args.index("--stage") + 1],
                unit_id=args[args.index("--unit-id") + 1]))
            assert code == 0
            return json.loads(capsys.readouterr().out)
        if command == "read-redacted-text-bundle":
            doc_ids = [args[index + 1] for index, token in enumerate(args)
                       if token == "--doc-id"]
            assert dao.cmd_read_redacted_text_bundle(make_args(
                case_id=args[1], doc_id=doc_ids)) == 0
            return json.loads(capsys.readouterr().out)
        if command == "verify-evidence-references":
            path = args[args.index("--references-file") + 1]
            refs = json.loads(Path(path).read_text(encoding="utf-8"))["references"]
            return {"verified_references": [
                {**ref, "source_text_revision_sha256": None,
                 "redacted_text_sha256": "b" * 64} for ref in refs]}
        raise AssertionError(command)

    def write(args):
        command = args[0]
        if command == "write-driver-candidate":
            code = dao.cmd_write_driver_candidate(make_args(
                case_id=args[1], stage=args[args.index("--stage") + 1],
                unit_id=args[args.index("--unit-id") + 1],
                candidate_id=args[args.index("--candidate-id") + 1],
                data_file=args[args.index("--data-file") + 1],
                held_by=args[args.index("--held-by") + 1],
                run_id=args[args.index("--run-id") + 1]))
            assert code == 0, capsys.readouterr().out
            capsys.readouterr()
            return
        if command in ("write-contract", "write-driver-receipt"):
            return
        raise AssertionError(command)

    return read, write


def _seed_processed(isolated_dao, doc_ids, manifest=None):
    for doc_id in doc_ids:
        processed = isolated_dao / "data" / "processed" / CASE / doc_id
        processed.mkdir(parents=True, exist_ok=True)
        (processed / "redacted_text.md").write_text(_page_text(doc_id),
                                                    encoding="utf-8")
    case_dir = isolated_dao / "outputs" / CASE
    case_dir.mkdir(parents=True, exist_ok=True)
    # The real read-redacted-text-bundle resolves documents through the
    # on-disk manifest, so seeding it is what keeps this a DAO integration
    # test rather than a fake.
    dao.atomic_write_json(case_dir / "document_manifest.json", manifest)
    dao.atomic_write_json(case_dir / "_revision_index.json", {
        "case_id": CASE,
        "documents": [{"document_id": doc_id,
                       "current_revision_sha256": "revision-1",
                       "uid_scheme": "legacy"} for doc_id in doc_ids],
    })


# --------------------------------------------------------------------------
# denial-response initial extraction: one unit per insurer bundle
# --------------------------------------------------------------------------

BUNDLE_DOCS = ["DOC_001", "DOC_002"]


def _denial_manifest():
    return {"case_id": CASE, "documents": [
        {"document_id": "DOC_001", "file_name": "a.pdf", "source_file_name": "a.pdf",
         "source_page_start": 1, "source_page_end": 1,
         "document_type": "insurer_response",
         "downstream_disposition": "text_only_no_normalization"},
        {"document_id": "DOC_002", "file_name": "b.pdf", "source_file_name": "b.pdf",
         "source_page_start": 1, "source_page_end": 1,
         "document_type": "insurer_response",
         "downstream_disposition": "text_only_no_normalization"},
    ]}


class _DenialProvider:
    provider_name = "fixture"
    model_name = "fixture-model"

    def __init__(self):
        self.bundles_called: list[str] = []

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        doc_id = next(item for item in BUNDLE_DOCS if f"## {item} " in prompt)
        self.bundles_called.append(doc_id)
        text = f"Finding for {doc_id}."
        return SimpleNamespace(structured_output={
            "status": "success", "confidence": 0.9, "review_required": False,
            "warnings": [], "accepted_coverages": [],
            "denial_reasons": [{
                "reason_id": "DR_1", "decided_coverage": "Synthetic coverage",
                "decision_type": "denial", "payment_status": "unpaid",
                "taxonomy_code": "R04",
                "candidate_codes": [{"taxonomy_code": "R04", "confidence": 0.9}],
                "raw_reason_text": text,
                "insurer_claim_summary": "Synthetic insurer position",
                "grounds": {"contractual_basis": [], "medical_or_factual_basis": [],
                            "calculation_basis": []},
                "amounts": {"claimed_amount": None, "payable_amount": None,
                            "denied_amount": None, "reduction_amount": None,
                            "reduction_rate": None},
                "requested_documents": [], "policy_matches": [], "confidence": 0.9,
                "evidence_references": [
                    {"document_id": doc_id, "page": 1, "quote": text}],
                "review_required": False,
            }],
        })


def _run_denial(isolated_dao, make_args, capsys, monkeypatch, provider,
                *, workers=1, manifest=None):
    read, write = _dao_router(isolated_dao, make_args, capsys,
                              manifest=manifest or _denial_manifest(),
                              stage="denial_response")
    monkeypatch.setattr(denial_driver, "_dao_json", read)
    monkeypatch.setattr(denial_driver, "_dao_write", write)
    return denial_driver.run(case_id=CASE, held_by="denial-response", run_id=RUN,
                             provider=provider, workers=workers)


def test_denial_interrupted_bundle_is_the_only_one_rerun(isolated_dao, make_args,
                                                         capsys, monkeypatch):
    _seed_processed(isolated_dao, BUNDLE_DOCS, manifest=_denial_manifest())
    second_bundle = denial_driver.bundle_candidate_id([{"document_id": "DOC_002"}])
    provider = _DenialProvider()

    with driver_runtime.interrupt_at("denial_response", [second_bundle]):
        with pytest.raises(driver_runtime.ProviderInterrupted):
            _run_denial(isolated_dao, make_args, capsys, monkeypatch, provider)

    stored = isolated_dao / "outputs" / CASE / "_driver_receipts" / "denial_response" / "initial_extraction"
    assert [path.stem for path in stored.glob("*.json")] == [
        denial_driver.bundle_candidate_id([{"document_id": "DOC_001"}])]

    resumed = _DenialProvider()
    result = _run_denial(isolated_dao, make_args, capsys, monkeypatch, resumed)

    assert resumed.bundles_called == ["DOC_002"]
    assert result["reused_candidate_count"] == 1


def test_denial_changed_digest_invalidates_only_the_affected_bundle(
        isolated_dao, make_args, capsys, monkeypatch):
    _seed_processed(isolated_dao, BUNDLE_DOCS, manifest=_denial_manifest())
    _run_denial(isolated_dao, make_args, capsys, monkeypatch, _DenialProvider())

    (isolated_dao / "data" / "processed" / CASE / "DOC_001" / "redacted_text.md").write_text(
        "<<<PAGE page=1>>>\nFinding for DOC_001.\nAmended.\n", encoding="utf-8")

    provider = _DenialProvider()
    result = _run_denial(isolated_dao, make_args, capsys, monkeypatch, provider)

    assert provider.bundles_called == ["DOC_001"]
    assert result["reused_candidate_count"] == 1


def test_denial_unrelated_manifest_entry_change_reuses_unaffected_bundles(
        isolated_dao, make_args, capsys, monkeypatch):
    """Re-typing one bundle's member entry must not invalidate the other bundle."""
    _seed_processed(isolated_dao, BUNDLE_DOCS, manifest=_denial_manifest())
    _run_denial(isolated_dao, make_args, capsys, monkeypatch, _DenialProvider())

    amended = _denial_manifest()
    # DOC_002's own entry moves; DOC_001's bundle reads nothing from it.
    amended["documents"][1]["source_page_end"] = 4
    dao.atomic_write_json(
        isolated_dao / "outputs" / CASE / "document_manifest.json", amended)

    provider = _DenialProvider()
    result = _run_denial(isolated_dao, make_args, capsys, monkeypatch, provider,
                         manifest=amended)

    assert provider.bundles_called == ["DOC_002"]
    assert result["reused_candidate_count"] == 1


def test_denial_workers_two_persists_the_completed_bundle_before_returning(
        isolated_dao, make_args, capsys, monkeypatch):
    """With two workers, an interrupted bundle must not discard its sibling."""
    _seed_processed(isolated_dao, BUNDLE_DOCS, manifest=_denial_manifest())
    first_id = denial_driver.bundle_candidate_id([{"document_id": "DOC_001"}])
    second_id = denial_driver.bundle_candidate_id([{"document_id": "DOC_002"}])
    stored = (isolated_dao / "outputs" / CASE / "_driver_receipts"
              / "denial_response" / "initial_extraction")
    completed = threading.Event()

    def hook(stage, candidate_id):
        if candidate_id == second_id:
            assert completed.wait(timeout=10), "the first bundle never published"
            raise driver_runtime.ProviderInterrupted("test interruption")

    monkeypatch.setattr(driver_runtime, "interruption_hook", hook)

    original_publish = denial_driver._publish_candidate

    def publish(**kwargs):
        original_publish(**kwargs)
        if kwargs["candidate_id"] == first_id:
            completed.set()

    monkeypatch.setattr(denial_driver, "_publish_candidate", publish)

    with pytest.raises(driver_runtime.ProviderInterrupted):
        _run_denial(isolated_dao, make_args, capsys, monkeypatch,
                    _DenialProvider(), workers=2)

    assert [path.stem for path in stored.glob("*.json")] == [first_id]

    resumed = _DenialProvider()
    monkeypatch.setattr(driver_runtime, "interruption_hook", None)
    result = _run_denial(isolated_dao, make_args, capsys, monkeypatch, resumed,
                         workers=2)

    assert resumed.bundles_called == ["DOC_002"]
    assert result["reused_candidate_count"] == 1


# --------------------------------------------------------------------------
# DAO ownership of the candidate surface
# --------------------------------------------------------------------------

def test_write_contract_refuses_to_forge_a_candidate(isolated_dao, make_args,
                                                     capsys, tmp_path):
    payload = tmp_path / "forged.json"
    payload.write_text(json.dumps({"case_id": CASE}), encoding="utf-8")

    code = dao.cmd_write_contract(make_args(
        case_id=CASE, filename="_driver_receipts/denial_response/initial_extraction/DOC_001.json",
        data_file=str(payload), schema_name="driver_candidate.schema.json",
        held_by="tester", run_id=RUN, stage=None, purpose=None))

    assert code == 1
    assert "DAO-owned" in capsys.readouterr().out


def test_candidate_identity_must_match_its_arguments(isolated_dao, make_args,
                                                     capsys, tmp_path):
    payload = driver_runtime.make_candidate(
        case_id=CASE, run_id=RUN, stage="denial_response", unit_id="initial_extraction",
        candidate_id="DOC_001", input_digests={"redacted:DOC_001": "c" * 64},
        prompt_version="v1", response_schema_version="v1",
        provider_name="fixture", model_name="fixture-model", result={"ok": True})
    data_file = tmp_path / "candidate.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    code = dao.cmd_write_driver_candidate(make_args(
        case_id=CASE, stage="denial_response", unit_id="initial_extraction",
        candidate_id="DOC_002", data_file=str(data_file),
        held_by="tester", run_id=RUN))

    assert code == 1
    assert "identity must match" in capsys.readouterr().out


def test_missing_candidate_directory_reads_as_empty(isolated_dao, make_args, capsys):
    assert dao.cmd_read_driver_candidates(make_args(
        case_id=CASE, stage="denial_response", unit_id="initial_extraction")) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == {}
