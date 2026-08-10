"""Shared fixtures. Every dao.py filesystem test runs against a tmp_path,
never the real outputs/ or data/ -- see isolated_dao below.

schemas/ is NOT faked -- tests validate against the project's real schema
files, since that's the actual contract being tested.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pytest

import dao
import llm_providers
import policy_uid_resolver


@pytest.fixture
def isolated_dao(tmp_path, monkeypatch):
    """Points dao.py's module-level OUTPUTS/DATA at a tmp dir for this test.

    dao's cmd_* functions read these as globals at call time (not bound at
    import time), so monkeypatching the module attributes redirects every
    case_dir()/processed_dir() call without touching the real project tree.
    """
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    return tmp_path


@pytest.fixture
def case_id():
    return "CASE_009"


@pytest.fixture
def run_id():
    return "RUN_20260712_001"


@pytest.fixture
def canonicalize():
    """Drive a seeded document through the real migration flow to canonical_v1.

    P0-3 makes canonical_v1 the only state new policy work may be written in,
    so any test that exercises a policy-layer write path needs its document
    genuinely registered. This runs the actual DAO commands -- register the
    source text, hash the raw file, activate -- rather than hand-writing a
    revision index, so a fixture cannot reach a state the production path
    cannot. The same helper is what P0-3's own tests use, so a fixture and the
    validator can never agree on a state neither could really produce.

    Requires the case's manifest to already name the document with a
    `file_path` under `data/raw/`, and that raw file to exist.
    """
    from pathlib import Path

    def _canonicalize(make_args, isolated_dao, case_id, doc_id, text,
                      held_by="document-pipeline", run_id="RUN_20260728_001"):
        text_file = Path(isolated_dao) / f"_canonicalize_{case_id}_{doc_id}.md"
        text_file.write_text(text, encoding="utf-8")
        assert dao.cmd_write_redacted_text(make_args(
            case_id=case_id, doc_id=doc_id, text_file=str(text_file),
            held_by=held_by, run_id=run_id)) == 0
        assert dao.cmd_record_source_digest(make_args(
            case_id=case_id, doc_id=doc_id, held_by=held_by,
            run_id=run_id, expect=None)) == 0
        assert dao.cmd_enable_canonical_uids(make_args(
            case_id=case_id, doc_id=doc_id, held_by=held_by,
            run_id=run_id)) == 0
        assert dao.uid_scheme_for(case_id, doc_id) == "canonical_v1"

    return _canonicalize


@pytest.fixture
def segment_pdf():
    """Build a real PDF whose printed page numbers are genuinely on the pages.

    P0-6's whole point is that the DAO reads the parent PDF itself, so a test
    that stubs the read proves nothing about the gate. Every P0-6 test therefore
    runs against an actual pymupdf-rendered file.

    `body_for(logical)` supplies each page's text; the printed marker
    'N / TOTAL' is appended for the pages that should carry one.
    `unnumbered` lists logical pages deliberately printed WITHOUT a marker (the
    "no printed number" case, which must block rather than fall back to the
    offset).
    """
    import fitz

    def _build(path, *, total_logical, offset=7, body_for=None,
               unnumbered=(), front_matter_body="표지"):
        """Physical page = logical + offset. The first `offset` physical pages
        are unnumbered front matter, exactly like CASE_030's real policy PDF."""
        # ASCII keeps the generated PDF's embedded-text layer deterministic
        # with PyMuPDF's built-in test font. Tests for Korean extraction live
        # at the OCR/document layer; these fixtures test provenance identity.
        body_for = body_for or (lambda lp: f"Clause {lp} body")
        doc = fitz.open()
        for _ in range(offset):
            page = doc.new_page()
            page.insert_text((72, 72), front_matter_body, fontsize=11)
        for logical in range(1, total_logical + 1):
            page = doc.new_page()
            page.insert_text((72, 72), body_for(logical), fontsize=11)
            if logical not in unnumbered:
                page.insert_text(
                    (72, 700), f"{logical} / {total_logical}", fontsize=9)
        doc.save(str(path))
        doc.close()
        return path

    return _build


@pytest.fixture
def register_derivation():
    """Run the real `register-segment-derivation` command.

    Tests use this rather than hand-writing a page_map, because hand-writing one
    is exactly what P0-6 makes impossible -- a fixture that could do it would be
    testing a path production cannot reach.
    """
    def _register(make_args, case_id, doc_id, pages, page_offset,
                  held_by="document-pipeline", run_id="RUN_20260728_001",
                  parent_document_id=None, page_map_file=None,
                  expect_parent_sha256=None):
        return dao.cmd_register_segment_derivation(make_args(
            case_id=case_id, doc_id=doc_id, pages=pages,
            page_offset=page_offset, held_by=held_by, run_id=run_id,
            parent_document_id=parent_document_id,
            page_map_file=page_map_file,
            expect_parent_sha256=expect_parent_sha256))

    return _register


@pytest.fixture
def make_args():
    """Builds an argparse.Namespace-like object for calling dao's cmd_*
    functions directly, without shelling out. Pass only the overrides a
    given test cares about; everything else defaults to None so a cmd_*
    function that ignores an unused attribute doesn't need it stubbed.
    """
    from types import SimpleNamespace

    def _make(**overrides):
        defaults = dict(
            case_id="CASE_009", doc_id="DOC_001", run_id="RUN_20260712_001",
            held_by="test-agent", purpose=None, stage=None,
            filename=None, data_file=None, schema_name=None,
            page=None, text_file=None, file_name=None, status=None,
            reviewer=None, reason=None, doc_path=None,
            topic=None, sources_file=None, conflict_id=None, verdict=None, note=None,
            caller_stage=None, description=None, version=None, fields_file=None,
            expect=None, pages=None, page_offset=None, page_map_file=None,
            parent_document_id=None, expect_parent_sha256=None,
            artifact_kind=None, artifact_id=None, target_key=None,
            decision=None, document_id=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    return _make
