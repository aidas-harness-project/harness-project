"""document_assembly.py -- render() plus the full CLI path (locking, atomic
write, sidecar schema validation) added this session after it was found
bypassing the DAO entirely. Regression coverage for the three real bugs
found while fixing it: the DAO-bypass itself, schema_name_for() not
resolving *.evidence.json, and render() emitting page: null.
"""
import json
from types import SimpleNamespace

import pytest

import document_assembly as da
import dao


@pytest.fixture
def isolated_da(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    return tmp_path


def test_render_placeholder_reference_count_must_match():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "H", "content": "one {{E}} two {{E}}",
         "evidence_references": [{"document_id": "DOC_001", "quote": "q"}]},  # only 1, content has 2
    ]}
    with pytest.raises(ValueError):
        da.render(spec)


def test_render_tags_assigned_sequentially_across_sections():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "first {{E}}", "evidence_references": [{"document_id": "DOC_001", "quote": "q1"}]},
        {"heading": "B", "content": "second {{E}}", "evidence_references": [{"document_id": "DOC_002", "quote": "q2"}]},
    ]}
    text, sidecar = da.render(spec)
    assert "[E1]" in text and "[E2]" in text
    assert [c["tag"] for c in sidecar["citations"]] == ["E1", "E2"]


def test_render_omits_page_key_when_absent():
    """Regression: used to emit page: null, which fails
    evidence_sidecar.schema.json's integer-only page type."""
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "text {{E}}", "evidence_references": [{"document_id": "DOC_001", "quote": "q"}]},
    ]}
    _, sidecar = da.render(spec)
    assert "page" not in sidecar["citations"][0]


def test_render_keeps_page_when_present():
    spec = {"output_path": "x.md", "sections": [
        {"heading": "A", "content": "text {{E}}", "evidence_references": [{"document_id": "DOC_001", "page": 3, "quote": "q"}]},
    ]}
    _, sidecar = da.render(spec)
    assert sidecar["citations"][0]["page"] == 3


def _write_sections_file(tmp_path, spec):
    p = tmp_path / "sections.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


def _run_main(sections_file, held_by="draft-report", run_id="RUN_X"):
    import sys
    argv = sys.argv
    sys.argv = ["document_assembly.py", "--sections-file", sections_file, "--held-by", held_by, "--run-id", run_id]
    try:
        da.main()
    finally:
        sys.argv = argv


def test_full_render_writes_md_and_valid_sidecar_and_releases_lock(isolated_da):
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "claim filed"}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)

    _run_main(sections_file)

    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    sidecar_path = out_path.with_suffix(".evidence.json")
    assert out_path.exists()
    assert sidecar_path.exists()
    assert not out_path.with_name(out_path.name + ".lock").exists()

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    assert validate_instance(sidecar, "evidence_sidecar.schema.json", schemas, registry) == []


def test_locked_target_is_not_rendered(isolated_da, monkeypatch):
    """document_assembly.py's lock now blocks-and-waits like everywhere else
    in the DAO -- keep the wait window tiny so this test doesn't actually
    sit for P5's real 15-minute cap before failing."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.03)
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "quote": "claim filed"}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)
    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    dao.acquire_lock(out_path, "someone-else", "RUN_OTHER", "holding")

    with pytest.raises(SystemExit):
        _run_main(sections_file)

    assert not out_path.exists()


def test_no_files_written_when_sidecar_would_fail_schema(isolated_da):
    """A reference with an empty quote fails evidence_sidecar.schema.json's
    minLength:1 -- neither the .md nor the sidecar should land on disk."""
    spec = {"output_path": "outputs/CASE_009/draft_report_v1.md", "sections": [
        {"heading": "1. Overview", "content": "Claim {{E}} filed.",
         "evidence_references": [{"document_id": "DOC_001", "quote": ""}]},
    ]}
    sections_file = _write_sections_file(isolated_da, spec)

    with pytest.raises(SystemExit):
        _run_main(sections_file)

    out_path = isolated_da / "outputs" / "CASE_009" / "draft_report_v1.md"
    assert not out_path.exists()
    assert not out_path.with_suffix(".evidence.json").exists()
