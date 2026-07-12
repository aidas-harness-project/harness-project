"""dao.py's read-evidence-tags -- the check critic.md uses instead of
manually re-deriving orphaned/unused [E#] tags from the raw files."""
import json

import dao


def _write_doc_and_sidecar(case_dir, doc_text, citations):
    doc_path = case_dir / "outputs" / "CASE_009" / "draft_report_v1.md"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(doc_text, encoding="utf-8")
    sidecar_path = doc_path.with_suffix(".evidence.json")
    sidecar_path.write_text(json.dumps({"document_path": str(doc_path), "citations": citations}), encoding="utf-8")
    return doc_path


def test_consistent_tags_and_citations(isolated_dao, make_args):
    doc_path = _write_doc_and_sidecar(
        isolated_dao, "Claim filed [E1]. Treated at hospital [E2].",
        [{"tag": "E1", "document_id": "DOC_001", "quote": "q1"},
         {"tag": "E2", "document_id": "DOC_001", "quote": "q2"}],
    )
    rc = dao.cmd_read_evidence_tags(make_args(doc_path=str(doc_path)))
    assert rc == 0


def test_orphaned_tag_detected(isolated_dao, make_args):
    """A [E#] in the text with no matching sidecar entry -- P1 fabrication risk."""
    doc_path = _write_doc_and_sidecar(
        isolated_dao, "Claim filed [E1]. Also [E2].",
        [{"tag": "E1", "document_id": "DOC_001", "quote": "q1"}],
    )
    rc = dao.cmd_read_evidence_tags(make_args(doc_path=str(doc_path)))
    assert rc == 1


def test_unused_citation_detected(isolated_dao, make_args):
    """A sidecar entry never referenced by a tag in the document."""
    doc_path = _write_doc_and_sidecar(
        isolated_dao, "Claim filed [E1].",
        [{"tag": "E1", "document_id": "DOC_001", "quote": "q1"},
         {"tag": "E2", "document_id": "DOC_001", "quote": "q2"}],
    )
    rc = dao.cmd_read_evidence_tags(make_args(doc_path=str(doc_path)))
    assert rc == 1


def test_missing_document_reports_not_found(isolated_dao, make_args):
    rc = dao.cmd_read_evidence_tags(make_args(doc_path=str(isolated_dao / "nope.md")))
    assert rc == 1
