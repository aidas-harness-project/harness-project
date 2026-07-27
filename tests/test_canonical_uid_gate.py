"""Part 11J commit c: the canonical_v1 gate, end to end through the DAO.

test_policy_uid.py covers the derivation. This covers the switch: what
activating canonical_v1 requires, that it cannot be undone, and that once it is
on, a policy-layer contract must state which registered source revision it was
derived from -- checked against the current pointer, not believed.

The legacy path is tested just as deliberately. Making the binding
unconditionally required would strand every pre-11J artifact and destroy the
audit trail this part exists to build, so legacy documents stay readable; what
they must NOT do is silently pass as new canonical work.
"""
import hashlib
import json

import pytest

import dao


TEXT = "<<<PAGE page=1>>>\n제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
RAW = b"%PDF-1.7 immutable policy source"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture
def case(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_005",
            "file_name": "DOC_005.pdf",
            "file_path": "data/raw/CASE_030/DOC_005.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
            "extraction_method": "embedded_text",
        }],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_005.pdf").write_bytes(RAW)
    return out


def _register_text(make_args, isolated_dao, text=TEXT, name="t.md"):
    path = isolated_dao / name
    path.write_text(text, encoding="utf-8")
    return dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(path),
        held_by="document-pipeline", run_id="RUN_20260724_001"))


def _record_digest(make_args):
    return dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260724_001", expect=None))


def _enable(make_args):
    return dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260724_001"))


# --- activation preconditions ---------------------------------------------

def test_cannot_enable_without_a_recorded_source_digest(case, isolated_dao,
                                                        make_args, capsys):
    _register_text(make_args, isolated_dao)
    assert _enable(make_args) == 1
    assert "source_pdf_sha256 is not recorded" in capsys.readouterr().out


def test_cannot_enable_without_a_registered_revision(case, make_args, capsys):
    """Verifying UIDs against unregistered text would just re-derive whatever
    happens to be on disk."""
    assert _record_digest(make_args) == 0
    assert _enable(make_args) == 1
    assert "no source-text revision is registered" in capsys.readouterr().out


def test_cannot_enable_when_the_raw_source_no_longer_matches(
        case, isolated_dao, make_args, capsys):
    _register_text(make_args, isolated_dao)
    assert _record_digest(make_args) == 0
    (isolated_dao / "data" / "raw" / "CASE_030" / "DOC_005.pdf").write_bytes(
        b"%PDF-1.7 a different file entirely")
    assert _enable(make_args) == 1
    assert "does not match the registered raw source" in capsys.readouterr().out


def test_a_segment_without_a_page_map_cannot_be_enabled(case, isolated_dao,
                                                        make_args, capsys):
    """Canonical identity is keyed to the PHYSICAL page of the immutable
    parent, which a segment cannot resolve without a page map.

    The manifest schema already makes page_map required on a segment, so this
    state is unreachable through a valid manifest -- the check in
    enable-canonical-uids is a second, independent floor rather than the only
    one. Asserted directly against the command's precondition logic, because
    routing it through a manifest write would only re-test the schema.
    """
    _register_text(make_args, isolated_dao)
    assert _record_digest(make_args) == 0
    # Written past the schema deliberately: this is the state the DAO must
    # still refuse if a manifest ever reached it by another route.
    manifest = json.loads(
        (case / "document_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["documents"][0]
    entry.update({"document_role": "segment", "source_document_id": "DOC_001"})
    entry.pop("page_map", None)
    (case / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _enable(make_args) == 1
    assert "no page_map" in capsys.readouterr().out


def test_enabling_succeeds_once_every_precondition_holds(case, isolated_dao,
                                                         make_args):
    _register_text(make_args, isolated_dao)
    assert _record_digest(make_args) == 0
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    assert _enable(make_args) == 0
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"


def test_canonical_v1_is_a_one_way_door(case, isolated_dao, make_args):
    """No disable command exists, and the scheme field is sealed against every
    agent-facing write path -- an off-switch would make the guarantee a
    bypass."""
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    assert not any(
        "disable" in name and "canonical" in name for name in dir(dao))
    # Re-enabling is a no-op, not an error, and never a downgrade.
    assert _enable(make_args) == 0
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"


# --- the contract binding --------------------------------------------------

def _inventory(binding=None):
    data = {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "boundaries": [{
            "boundary_uid": "PB-1111111111111111",
            "boundary_level": "article",
            "label": "제3조",
            "disposition": "normalized",
            "normalized_mappings": [{
                "clause_uid": "PC-1111111111111111",
                "display_clause_id": "C-1",
                "bucket": "clause",
                "condition_uid": None,
            }],
            "reason": None,
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": "PS-1111111111111111",
            "page": 1,
            "start_char": 0,
            "end_char": 27,
            "quote": "제3조(보험금의 지급) 회사는 보험금을 지급합니다.",
            "disposition": "boundary",
            "boundary_uid": "PB-1111111111111111",
            "exclusion_reason": None,
        }],
    }
    if binding is not None:
        data["source_text_revision"] = binding
    return data


def _write_inventory(isolated_dao, make_args, data):
    data_file = isolated_dao / "inv.json"
    _write_json(data_file, data)
    return dao.cmd_write_contract(make_args(
        case_id="CASE_030",
        filename="policy_boundary_inventory_DOC_005.json",
        data_file=str(data_file),
        schema_name="policy_boundary_inventory.schema.json",
        held_by="policy-pipeline", run_id="RUN_20260724_001", stage=None))


def test_legacy_documents_do_not_need_a_revision_binding(case, isolated_dao,
                                                         make_args):
    """Pre-11J artifacts stay writable/readable until canonical is switched on
    -- requiring the binding unconditionally would strand them all and destroy
    the audit trail this part builds."""
    _register_text(make_args, isolated_dao)
    assert dao._source_revision_binding_errors(
        "CASE_030", "policy_boundary_inventory_DOC_005.json",
        _inventory()) == []


def test_canonical_documents_must_state_their_source_revision(
        case, isolated_dao, make_args):
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    errors = dao._source_revision_binding_errors(
        "CASE_030", "policy_boundary_inventory_DOC_005.json", _inventory())
    assert any("source_text_revision is missing" in e for e in errors), errors


def test_a_stale_revision_binding_is_refused(case, isolated_dao, make_args):
    """The exact failure the binding exists to catch: the source text was
    revised after the contract was derived from it."""
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    stale = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()

    _register_text(make_args, isolated_dao,
                   text=TEXT.replace("지급합니다", "지급하지 않습니다"),
                   name="t2.md")
    errors = dao._source_revision_binding_errors(
        "CASE_030", "policy_boundary_inventory_DOC_005.json",
        _inventory({"documents": [{"document_id": "DOC_005",
                                   "revision_sha256": stale}]}))
    assert any("is stale" in e for e in errors), errors


def test_a_current_binding_clears_this_gate_and_hands_off(
        case, isolated_dao, make_args, capsys):
    """A current binding must stop being the reason a write is refused.

    Asserting rc == 0 here would need a fully valid inventory -- exact span
    quotes, complete page coverage, resolvable clause mappings -- and would
    then be a test of Parts 11C/11E rather than of this binding. What belongs
    here is that this gate is satisfied and the later checks are what speak.
    """
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    current = dao.revision_entry_for(
        "CASE_030", "DOC_005")["current_revision_sha256"]
    bound = _inventory({"documents": [{"document_id": "DOC_005",
                                       "revision_sha256": current}]})
    assert dao._source_revision_binding_errors(
        "CASE_030", "policy_boundary_inventory_DOC_005.json", bound) == []
    _write_inventory(isolated_dao, make_args, bound)
    output = capsys.readouterr().out
    assert "not bound to the current registered source revision" not in output


def test_write_contract_refuses_an_unbound_canonical_contract(
        case, isolated_dao, make_args):
    """End to end: not just the checker, the actual write path."""
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    assert not (case / "policy_boundary_inventory_DOC_005.json").exists()


def test_a_binding_that_omits_a_referenced_document_is_refused(
        case, isolated_dao, make_args):
    _register_text(make_args, isolated_dao)
    _record_digest(make_args)
    assert _enable(make_args) == 0
    errors = dao._source_revision_binding_errors(
        "CASE_030", "policy_boundary_inventory_DOC_005.json",
        _inventory({"documents": [{"document_id": "DOC_999",
                                   "revision_sha256": "0" * 64}]}))
    assert any("does not cover DOC_005" in e for e in errors), errors
