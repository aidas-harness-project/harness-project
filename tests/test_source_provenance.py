"""Part 11J commit b: DAO-owned source provenance.

Four fields decide whether a canonical UID means anything, and all four were
previously writable by whoever wrote the manifest:

  source_pdf_sha256   the FIRST input to every canonical UID
  uid_scheme          which verification the UIDs are held to
  uid_stability       whether re-extraction may legitimately move a UID
  the revision list   which bytes are authoritative, and what preceded them

An agent that can set these can decide what its own UIDs are checked against,
which makes the check ceremonial. The tests here are adversarial: each one
attempts the specific bypass the seal exists to close.
"""
import hashlib
import json

import pytest

import dao
import source_provenance as sp


TEXT_V1 = "<<<PAGE page=1>>>\n제1조 보험금을 지급합니다.\n<<<PAGE page=2>>>\n제2조 면책.\n"
TEXT_V2 = "<<<PAGE page=1>>>\n제1조 보험금을 지급합니다.\n<<<PAGE page=2>>>\n제2조 면책사유.\n"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _manifest(**overrides):
    entry = {
        "document_id": "DOC_005",
        "file_name": "DOC_005.pdf",
        "file_path": "data/raw/CASE_030/DOC_005.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "ocr_status": "completed",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
        "extraction_method": "embedded_text",
    }
    entry.update(overrides)
    return {"case_id": "CASE_030", "documents": [entry]}


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture
def case(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json", _manifest())
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_005.pdf").write_bytes(b"%PDF-1.7 immutable source bytes")
    return out


def _write_text(make_args, isolated_dao, text, name="t.md"):
    path = isolated_dao / name
    path.write_text(text, encoding="utf-8")
    return dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(path),
        held_by="document-pipeline", run_id="RUN_20260724_001"))


# --- uid_stability is derived, never declared ------------------------------

def test_uid_stability_is_derived_from_extraction_method():
    assert sp.derive_uid_stability(
        {"extraction_method": "embedded_text"}) == "deterministic_source"
    for method in ("ocr", "mixed"):
        assert sp.derive_uid_stability(
            {"extraction_method": method}) == "ocr_derived"


def test_unknown_extraction_method_claims_nothing():
    """'unknown' rather than a convenient default. Claiming determinism nobody
    established would make an expected OCR difference look like a bug, and
    claiming non-determinism would excuse a real one."""
    assert sp.derive_uid_stability({}) == "unknown"
    assert sp.derive_uid_stability(None) == "unknown"
    assert sp.derive_uid_stability(
        {"extraction_method": "handwave"}) == "unknown"


def test_an_agent_cannot_declare_its_own_stability(case, isolated_dao,
                                                   make_args):
    """The manifest is agent-written, so a self-declared stability would be
    the self-certification D2 exists to prevent -- one gate over."""
    assert _write_text(make_args, isolated_dao, TEXT_V1) == 0
    entry = dao.revision_entry_for("CASE_030", "DOC_005")
    assert entry["revisions"][0]["uid_stability"] == "deterministic_source"

    data_file = isolated_dao / "m.json"
    _write_json(data_file, _manifest(uid_stability="deterministic_source",
                                     extraction_method="ocr"))
    rc = dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="document_manifest.json",
        data_file=str(data_file), schema_name="document_manifest.schema.json",
        held_by="document-pipeline", run_id="RUN_20260724_001", stage=None))
    assert rc == 1


# --- the protected-field seal ---------------------------------------------

def test_insertion_modification_deletion_and_rollback_are_all_refused():
    previous = {"source_pdf_sha256": "a" * 64, "uid_scheme": "canonical_v1"}
    # insertion of a field the DAO never derived
    assert sp.protected_field_errors(None, {"source_pdf_sha256": "b" * 64})
    # modification
    assert sp.protected_field_errors(previous, {**previous,
                                                "source_pdf_sha256": "b" * 64})
    # deletion -- the bypass a value-only comparison would miss
    errors = sp.protected_field_errors(previous, {"uid_scheme": "canonical_v1"})
    assert any("may not be deleted" in e for e in errors), errors
    # rollback to a weaker earlier value
    assert sp.protected_field_errors(previous, {**previous,
                                                "uid_scheme": "legacy"})
    # an untouched entry is fine
    assert sp.protected_field_errors(previous, dict(previous)) == []


def test_write_contract_refuses_to_change_a_sealed_field(case, isolated_dao,
                                                         make_args):
    args = make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect=None)
    assert dao.cmd_record_source_digest(args) == 0
    recorded = json.loads(
        (case / "document_manifest.json").read_text(encoding="utf-8")
    )["documents"][0]["source_pdf_sha256"]

    data_file = isolated_dao / "m.json"
    _write_json(data_file, _manifest(source_pdf_sha256="0" * 64))
    assert dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="document_manifest.json",
        data_file=str(data_file), schema_name="document_manifest.schema.json",
        held_by="document-pipeline", run_id="RUN_20260724_001",
        stage=None)) == 1
    # unchanged on disk
    assert json.loads(
        (case / "document_manifest.json").read_text(encoding="utf-8")
    )["documents"][0]["source_pdf_sha256"] == recorded


def test_write_contract_refuses_to_delete_an_entry_carrying_sealed_fields(
        case, isolated_dao, make_args):
    """Deleting the whole entry would take its sealed provenance with it."""
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect=None)) == 0
    data_file = isolated_dao / "m.json"
    _write_json(data_file, {"case_id": "CASE_030", "documents": []})
    assert dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="document_manifest.json",
        data_file=str(data_file), schema_name="document_manifest.schema.json",
        held_by="document-pipeline", run_id="RUN_20260724_001",
        stage=None)) == 1


def test_patch_manifest_document_refuses_a_sealed_field(case, make_args):
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect=None)) == 0
    ok, message = dao.patch_manifest_document(
        "CASE_030", "DOC_005", {"source_pdf_sha256": "0" * 64},
        "document-pipeline", "RUN_20260724_001")
    assert ok is False
    assert "DAO-owned" in message


def test_an_ordinary_patch_still_works(case, make_args):
    ok, message = dao.patch_manifest_document(
        "CASE_030", "DOC_005", {"ocr_quality": "high"},
        "document-pipeline", "RUN_20260724_001")
    assert ok is True, message


# --- source_pdf_sha256 is hashed by the DAO, not submitted -----------------

def test_the_dao_hashes_the_registered_file_itself(case, make_args):
    expected = hashlib.sha256(
        b"%PDF-1.7 immutable source bytes").hexdigest()
    assert dao.registered_source_pdf_sha256("CASE_030", "DOC_005") == expected
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect=None)) == 0
    assert json.loads(
        (case / "document_manifest.json").read_text(encoding="utf-8")
    )["documents"][0]["source_pdf_sha256"] == expected


def test_a_submitted_digest_that_disagrees_is_refused(case, make_args):
    """The disagreement is the finding: the file this document was extracted
    from is not the file the case registered."""
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect="0" * 64)) == 1
    assert "source_pdf_sha256" not in json.loads(
        (case / "document_manifest.json").read_text(encoding="utf-8")
    )["documents"][0]


def test_a_changed_raw_source_is_a_finding_not_an_overwrite(case, make_args,
                                                            isolated_dao):
    args = make_args(case_id="CASE_030", doc_id="DOC_005",
                     held_by="document-pipeline", run_id="RUN_20260724_001",
                     expect=None)
    assert dao.cmd_record_source_digest(args) == 0
    (isolated_dao / "data" / "raw" / "CASE_030" / "DOC_005.pdf").write_bytes(
        b"%PDF-1.7 DIFFERENT bytes")
    assert dao.cmd_record_source_digest(args) == 1


# --- the revision index ----------------------------------------------------

def test_revisions_are_appended_with_derived_provenance(case, isolated_dao,
                                                        make_args):
    assert _write_text(make_args, isolated_dao, TEXT_V1, "a.md") == 0
    assert _write_text(make_args, isolated_dao, TEXT_V2, "b.md") == 0

    entry = dao.revision_entry_for("CASE_030", "DOC_005")
    assert len(entry["revisions"]) == 2
    v1 = hashlib.sha256(TEXT_V1.encode("utf-8")).hexdigest()
    v2 = hashlib.sha256(TEXT_V2.encode("utf-8")).hexdigest()
    assert entry["current_revision_sha256"] == v2
    assert entry["revisions"][1]["supersedes"] == v1
    # A new document starts legacy -- canonical is only reachable through the
    # dedicated verified command, never by default.
    assert entry["uid_scheme"] == "legacy"
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    # The superseded revision is still on disk, so its UIDs stay recomputable.
    assert dao.revision_file_path("CASE_030", "DOC_005", v1).exists()
    assert dao.revision_file_path("CASE_030", "DOC_005", v2).exists()


def test_page_digests_localize_a_change_to_the_page_that_moved(case,
                                                               isolated_dao,
                                                               make_args):
    """A whole-document hash only says 'something differs' -- the granularity
    that made a one-character fix look document-wide."""
    assert _write_text(make_args, isolated_dao, TEXT_V1, "a.md") == 0
    assert _write_text(make_args, isolated_dao, TEXT_V2, "b.md") == 0
    revisions = dao.revision_entry_for("CASE_030", "DOC_005")["revisions"]
    before = {p["page"]: p["sha256"] for p in revisions[0]["page_text_sha256"]}
    after = {p["page"]: p["sha256"] for p in revisions[1]["page_text_sha256"]}
    assert before[1] == after[1]   # untouched page
    assert before[2] != after[2]   # the edited one


def test_extractor_profile_change_is_detectable():
    a = sp.extractor_profile({"extraction_method": "embedded_text"}, "dao")
    b = sp.extractor_profile({"extraction_method": "ocr"}, "dao")
    assert a["profile_sha256"] != b["profile_sha256"]
    assert sp.extractor_profile(
        {"extraction_method": "embedded_text"}, "dao")["profile_sha256"] == \
        a["profile_sha256"]


# --- canonical_v1 is a one-way door ---------------------------------------

def test_uid_scheme_cannot_be_downgraded():
    assert sp.scheme_transition_errors("legacy", "canonical_v1") == []
    assert sp.scheme_transition_errors("canonical_v1", "canonical_v1") == []
    errors = sp.scheme_transition_errors("canonical_v1", "legacy")
    assert any("may not be downgraded" in e for e in errors), errors
    assert sp.scheme_transition_errors(None, "nonsense")


def test_revision_history_is_append_only():
    previous = {"uid_scheme": "legacy",
                "revisions": [{"revision_sha256": "a" * 64},
                              {"revision_sha256": "b" * 64}]}
    # truncation
    assert sp.revision_history_errors(
        previous, {"uid_scheme": "legacy",
                   "revisions": [{"revision_sha256": "a" * 64}]})
    # prefix rewrite
    assert sp.revision_history_errors(
        previous, {"uid_scheme": "legacy",
                   "revisions": [{"revision_sha256": "z" * 64},
                                 {"revision_sha256": "b" * 64}]})
    # honest append
    assert sp.revision_history_errors(
        previous, {"uid_scheme": "legacy",
                   "revisions": previous["revisions"] + [
                       {"revision_sha256": "c" * 64}]}) == []
