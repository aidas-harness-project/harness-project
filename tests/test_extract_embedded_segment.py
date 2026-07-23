"""Part 8: deterministic embedded-text segment extraction.

Tests the pure logic (page-spec parsing, embedded read + fail-loud on empty,
marker assembly) with a stubbed PDF and stubbed DAO -- no real PDF or real
dao.py subprocess.
"""
import sys
import types

import pytest

import extract_embedded_segment as ees


def test_parse_pages_range():
    assert ees._parse_pages("120-123") == [120, 121, 122, 123]


def test_parse_pages_mixed_and_dedup():
    assert ees._parse_pages("57,58,118-119,58") == [57, 58, 118, 119]


class _StubPage:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class _StubDoc:
    def __init__(self, texts):
        # texts: list indexed by physical (0-based)
        self._pages = [_StubPage(t) for t in texts]
        self.page_count = len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]

    def close(self):
        pass


@pytest.fixture
def stub_fitz(monkeypatch):
    """Install a fake `fitz` module whose open() returns a preset doc."""
    holder = {}

    def _open(path):
        return holder["doc"]

    fake = types.SimpleNamespace(open=_open)
    monkeypatch.setitem(sys.modules, "fitz", fake)
    return holder


def test_read_embedded_pages_offset(stub_fitz):
    # physical = logical + 7; put identifiable text at physical 128/129 (logical 121/122)
    texts = [""] * 130
    texts[127] = "별표1 page"      # physical 128 -> logical 121
    texts[128] = "판정기준 page"    # physical 129 -> logical 122
    stub_fitz["doc"] = _StubDoc(texts)

    out = ees.read_embedded_pages("dummy.pdf", [121, 122], page_offset=7)

    assert out == {121: "별표1 page", 122: "판정기준 page"}


def test_read_embedded_pages_empty_layer_fails_loud(stub_fitz):
    texts = [""] * 130
    texts[127] = "   \n \n "  # physical 128 -> logical 121, whitespace only
    stub_fitz["doc"] = _StubDoc(texts)

    with pytest.raises(ValueError, match="empty embedded text layer"):
        ees.read_embedded_pages("dummy.pdf", [121], page_offset=7)


def test_read_embedded_pages_out_of_range_fails(stub_fitz):
    stub_fitz["doc"] = _StubDoc(["x"] * 10)
    with pytest.raises(ValueError, match="outside the PDF"):
        ees.read_embedded_pages("dummy.pdf", [100], page_offset=7)


def test_extract_segment_assembles_markers_and_sha(stub_fitz, monkeypatch):
    import hashlib

    texts = [""] * 20
    texts[7] = "PAGE ONE\n"    # physical 8 -> logical 1
    texts[8] = "PAGE TWO\n"    # physical 9 -> logical 2
    stub_fitz["doc"] = _StubDoc(texts)

    calls = []

    def _fake_dao(*args):
        calls.append(args)
        return ""

    monkeypatch.setattr(ees, "_dao", _fake_dao)

    result = ees.extract_segment(
        "CASE_030", "DOC_009", "dummy.pdf", [1, 2], page_offset=7,
        held_by="tester", run_id="RUN_1")

    # two write-page-text + one write-redacted-text
    kinds = [c[0] for c in calls]
    assert kinds.count("write-page-text") == 2
    assert kinds.count("write-redacted-text") == 1

    expected_redacted = "<<<PAGE page=1>>>\nPAGE ONE\n\n<<<PAGE page=2>>>\nPAGE TWO\n\n"
    expected_sha = hashlib.sha256(expected_redacted.encode("utf-8")).hexdigest()
    assert result["derived_text_sha256"] == expected_sha
    assert result["extraction_method"] == "embedded_text"
    assert result["cross_validation_mode"] == "deferred_poc"
