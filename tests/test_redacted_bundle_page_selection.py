"""A long policy can be read by the page, not only whole.

Measured on CASE_027's claim_analysis: the two policy bundles were 222,084 of
the stage's 272,655 characters of input -- 81% -- and the four checkpoint
contracts cited five pages of DOC_010 and none at all of DOC_009. The stage
spent 218,589 tokens, and tool time was 1.92s of 692s, so wall time is token
volume and that volume was overwhelmingly policy text nothing referenced.

There was no redacted page-level read to use instead. `read-page-text` serves
the PRE-redaction layer and is refused to analysis stages, so the agent tried
it four times, was refused, and fell back to the whole bundle -- visible in the
trace. `search-document-text` and `_document_index.json` both already report
page numbers; this is what makes those numbers actionable.

The revision hash still covers the FULL document, so a narrowed read verifies
exactly like a whole one. Narrowing is a delivery decision, never a second
source of truth.
"""
import json

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


POLICY = "".join(
    f"<<<PAGE page={n}>>>\n제{n}조(보상하는 손해) 본문 {n}\n" for n in range(1, 41))
SHORT = "<<<PAGE page=1>>>\n진 단 서\n우측 손목 골절\n"


def _seed(isolated_dao):
    case = isolated_dao / "outputs" / "CASE_027"
    case.mkdir(parents=True, exist_ok=True)
    (case / "document_manifest.json").write_text(json.dumps({
        "case_id": "CASE_027",
        "documents": [
            {"document_id": "DOC_010",
             "downstream_disposition": "automated_text_pipeline",
             "redacted_text_path":
                 "data/processed/CASE_027/DOC_010/redacted_text.md"},
            {"document_id": "DOC_011",
             "downstream_disposition": "automated_text_pipeline",
             "redacted_text_path":
                 "data/processed/CASE_027/DOC_011/redacted_text.md"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    for doc_id, text in (("DOC_010", POLICY), ("DOC_011", SHORT)):
        d = isolated_dao / "data" / "processed" / "CASE_027" / doc_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "redacted_text.md").write_text(text, encoding="utf-8")


def _doc(result, doc_id):
    return next(d for d in result["documents"] if d["document_id"] == doc_id)


# --- the narrowing itself ---------------------------------------------------

def test_pages_narrow_one_document(isolated_dao):
    _seed(isolated_dao)
    result = dao.read_redacted_text_bundle_data(
        "CASE_027", ["DOC_010"], {"DOC_010": {11, 35, 36}})
    doc = _doc(result, "DOC_010")
    assert [p["page"] for p in doc["pages"]] == [11, 35, 36]
    assert doc["total_page_count"] == 40
    assert doc["pages_omitted"] == 37


def test_narrowed_text_is_byte_identical_to_the_whole_read(isolated_dao):
    """Narrowing must deliver less, never something different -- otherwise a
    quote verified against a narrowed read could fail against the document."""
    _seed(isolated_dao)
    whole = _doc(dao.read_redacted_text_bundle_data("CASE_027", ["DOC_010"]),
                 "DOC_010")
    narrow = _doc(dao.read_redacted_text_bundle_data(
        "CASE_027", ["DOC_010"], {"DOC_010": {11, 35}}), "DOC_010")
    by_page = {p["page"]: p["text"] for p in whole["pages"]}
    assert all(p["text"] == by_page[p["page"]] for p in narrow["pages"])
    # The fixture must actually be big enough for narrowing to mean something.
    assert len(whole["pages"]) == 40 and len(narrow["pages"]) == 2


def test_revision_hash_still_covers_the_whole_document(isolated_dao):
    """The hash binds the revision, not the delivery. If narrowing changed it,
    every evidence reference taken from a narrowed read would fail
    verification against the document it actually came from."""
    _seed(isolated_dao)
    whole = _doc(dao.read_redacted_text_bundle_data("CASE_027", ["DOC_010"]),
                 "DOC_010")
    narrow = _doc(dao.read_redacted_text_bundle_data(
        "CASE_027", ["DOC_010"], {"DOC_010": {2}}), "DOC_010")
    assert narrow["redacted_text_sha256"] == whole["redacted_text_sha256"]


def test_unnarrowed_documents_in_the_same_call_come_back_whole(isolated_dao):
    """Per-document scoping: narrowing the policy must not truncate the
    diagnosis certificate sitting beside it in the same bundle."""
    _seed(isolated_dao)
    result = dao.read_redacted_text_bundle_data(
        "CASE_027", ["DOC_010", "DOC_011"], {"DOC_010": {3}})
    assert _doc(result, "DOC_010")["pages_omitted"] == 39
    assert _doc(result, "DOC_011")["pages_omitted"] == 0
    assert len(_doc(result, "DOC_011")["pages"]) == 1


def test_default_is_unchanged(isolated_dao):
    """Every existing caller passes no pages and must see what it always saw."""
    _seed(isolated_dao)
    doc = _doc(dao.read_redacted_text_bundle_data("CASE_027", ["DOC_010"]),
               "DOC_010")
    assert len(doc["pages"]) == 40
    assert doc["pages_omitted"] == 0
    assert doc["total_page_count"] == 40


# --- failing loud rather than delivering the wrong thing ---------------------

def test_a_page_the_document_lacks_is_refused(isolated_dao):
    """An empty result would read as 'that page holds nothing', which is a
    different and much worse claim than 'no such page'."""
    _seed(isolated_dao)
    with pytest.raises(ValueError, match="NO_SUCH_PAGE"):
        dao.read_redacted_text_bundle_data(
            "CASE_027", ["DOC_010"], {"DOC_010": {999}})


def test_pages_for_an_unrequested_document_is_refused(isolated_dao):
    """Otherwise the call silently delivers whole the bundle it meant to
    narrow -- the failure is invisible except as cost."""
    _seed(isolated_dao)
    with pytest.raises(ValueError, match="UNSCOPED_PAGES"):
        dao.read_redacted_text_bundle_data(
            "CASE_027", ["DOC_010"], {"DOC_011": {1}})


# --- the CLI spec parser ----------------------------------------------------

def test_page_spec_accepts_lists_and_ranges():
    assert dao._parse_pages_argument(["DOC_010=11,35-37,38"]) == {
        "DOC_010": {11, 35, 36, 37, 38}}


def test_repeated_pages_flags_merge_per_document():
    assert dao._parse_pages_argument(["DOC_010=1", "DOC_010=2", "DOC_011=5"]) == {
        "DOC_010": {1, 2}, "DOC_011": {5}}


@pytest.mark.parametrize("bad", [
    "DOC_010",        # no '='
    "DOC_010=",       # no pages
    "=1,2",           # no doc id
    "DOC_010=abc",    # not a number
    "DOC_010=0",      # pages are 1-based
    "DOC_010=7-3",    # inverted range
])
def test_malformed_page_specs_are_refused(bad):
    """A filter that silently drops a page produces an analysis missing
    evidence it believes it read."""
    with pytest.raises(ValueError, match="BAD_PAGES"):
        dao._parse_pages_argument([bad])


def test_no_pages_argument_means_no_narrowing():
    assert dao._parse_pages_argument(None) == {}
    assert dao._parse_pages_argument([]) == {}
