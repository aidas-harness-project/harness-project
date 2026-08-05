"""A superseded bundle's processed text must not be served to analysis stages.

`split` hands each child its slice of the parent's pages and retains the
parent as `superseded_bundle` for provenance. It does NOT delete the parent's
redacted_text.md, and the manifest records that parent's `redacted_text_path`
as null to say the text is no longer its to serve.

The DAO used to serve it anyway: both read paths checked the filesystem for
`expert_review_only` only, so the manifest's statement was advisory. That is
worse than a plain leak, because every page of the bundle is ALSO a page of
some child -- an analysis stage reading both sees one page twice and can count
it as two independent sources agreeing with each other. Found live by
consistency-check on CASE_909, which had to exclude DOC_005 by hand.
"""
import json

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


BUNDLE_TEXT = "<<<PAGE page=1>>>\n진 단 서\n환자는 우측 손목 골절상\n"
CHILD_TEXT = "<<<PAGE page=1>>>\n진 단 서\n환자는 우측 손목 골절상\n"


def _seed(isolated_dao, bundle_disposition="superseded_bundle"):
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "document_manifest.json").write_text(json.dumps({
        "case_id": "CASE_009",
        "documents": [
            {"document_id": "DOC_005",
             "downstream_disposition": bundle_disposition,
             # The manifest already says the bundle owns no text; the guard
             # exists because the file on disk contradicted that.
             "redacted_text_path": None},
            {"document_id": "DOC_007",
             "downstream_disposition": "automated_text_pipeline",
             "redacted_text_path":
                 "data/processed/CASE_009/DOC_007/redacted_text.md"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    for doc_id, text in (("DOC_005", BUNDLE_TEXT), ("DOC_007", CHILD_TEXT)):
        d = isolated_dao / "data" / "processed" / "CASE_009" / doc_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "redacted_text.md").write_text(text, encoding="utf-8")


def test_read_document_text_refuses_a_superseded_bundle(
        isolated_dao, make_args, capsys):
    _seed(isolated_dao)
    rc = dao.cmd_read_document_text(make_args(doc_id="DOC_005"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "SUPERSEDED_BUNDLE" in out
    # The refusal has to say what to do instead, or a caller will route around
    # it by opening the path directly.
    assert "children" in out


def test_read_document_text_still_serves_the_children(
        isolated_dao, make_args, capsys):
    """The guard must not cost the split its whole point."""
    _seed(isolated_dao)
    assert dao.cmd_read_document_text(make_args(doc_id="DOC_007")) == 0
    assert "redacted_text.md" in capsys.readouterr().out


def test_all_docs_search_skips_the_superseded_bundle(
        isolated_dao, make_args, capsys):
    """The duplication this prevents: bundle and child carry the SAME page, so
    an unfiltered sweep reports one occurrence as two."""
    _seed(isolated_dao)
    rc = dao.cmd_search_document_text(
        make_args(all_docs=True, doc_id=None, term="골절상", context=40))
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert "DOC_005" not in result["documents_searched"]
    assert "DOC_007" in result["documents_searched"]
    # One real occurrence, reported once.
    assert result["hit_count"] == 1


def test_a_normal_bundle_before_splitting_is_still_readable(
        isolated_dao, make_args):
    """Only the RETAINED, already-split bundle is refused. An unsplit bundle
    must stay readable -- segmentation reads its text to place boundaries."""
    _seed(isolated_dao, bundle_disposition="automated_text_pipeline")
    assert dao.cmd_read_document_text(make_args(doc_id="DOC_005")) == 0
