"""chunk_text.py -- the deterministic page-chunker for document-pipeline's
checkpoint 3. Closes the gap found in the end-to-end pipeline review: no
chunking tool existed, so page_chunks.schema.json's "verbatim -- not
re-summarized" requirement had no structural guarantee, only a prompting
instruction.
"""
import pytest

import chunk_text as ct


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "DATA", tmp_path / "data")
    return tmp_path / "data"


def _write_redacted(isolated_data, case_id, doc_id, text):
    d = isolated_data / "processed" / case_id / doc_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "redacted_text.md").write_text(text, encoding="utf-8")


def test_split_pages_recovers_exact_verbatim_text():
    text = "<<<PAGE page=1>>>\nfirst page\nsecond line\n<<<PAGE page=2>>>\nsecond page\n"
    pages = ct.split_pages(text)
    assert pages == [(1, "first page\nsecond line"), (2, "second page")]


def test_split_pages_handles_non_sequential_or_out_of_order_numbers():
    """Page numbers come from the marker itself, not position in the list --
    a chunker shouldn't silently assume 1,2,3,... order."""
    text = "<<<PAGE page=5>>>\ntext for page five\n<<<PAGE page=3>>>\ntext for page three\n"
    pages = ct.split_pages(text)
    assert pages == [(5, "text for page five"), (3, "text for page three")]


def test_split_pages_fails_loud_with_no_markers():
    with pytest.raises(SystemExit):
        ct.split_pages("just plain text, no markers at all")


def test_chunk_document_produces_one_chunk_per_page(isolated_data):
    _write_redacted(isolated_data, "CASE_009", "DOC_001",
                     "<<<PAGE page=1>>>\npage one\n<<<PAGE page=2>>>\npage two\n")

    chunks, next_id = ct.chunk_document("CASE_009", "DOC_001", chunk_id_start=1)

    assert len(chunks) == 2
    assert chunks[0] == {"chunk_id": "CHUNK_1", "document_id": "DOC_001", "page_start": 1, "page_end": 1, "text": "page one"}
    assert chunks[1] == {"chunk_id": "CHUNK_2", "document_id": "DOC_001", "page_start": 2, "page_end": 2, "text": "page two"}
    assert next_id == 3


def test_chunk_document_missing_redacted_text_fails_loud(isolated_data):
    with pytest.raises(SystemExit):
        ct.chunk_document("CASE_009", "DOC_999", chunk_id_start=1)


def test_chunk_ids_sequential_across_multiple_documents_no_collision(isolated_data):
    """This is the whole point of the case-scoped design -- one combined
    page_chunks.json, chunk_id must never repeat across documents."""
    _write_redacted(isolated_data, "CASE_009", "DOC_001", "<<<PAGE page=1>>>\na\n<<<PAGE page=2>>>\nb\n")
    _write_redacted(isolated_data, "CASE_009", "DOC_002", "<<<PAGE page=1>>>\nc\n")

    all_chunks = []
    next_id = 1
    for doc_id in ["DOC_001", "DOC_002"]:
        chunks, next_id = ct.chunk_document("CASE_009", doc_id, next_id)
        all_chunks.extend(chunks)

    ids = [c["chunk_id"] for c in all_chunks]
    assert ids == ["CHUNK_1", "CHUNK_2", "CHUNK_3"]
    assert len(set(ids)) == len(ids)
    assert all_chunks[2]["document_id"] == "DOC_002"
    assert all_chunks[2]["page_start"] == 1, "DOC_002's own page 1, not a continuation of DOC_001's page count"


def test_output_validates_against_page_chunks_schema(isolated_data):
    _write_redacted(isolated_data, "CASE_009", "DOC_001", "<<<PAGE page=1>>>\n검증 텍스트\n")
    chunks, _ = ct.chunk_document("CASE_009", "DOC_001", 1)

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tools"))
    from _validation import load_registry, validate_instance

    schemas, registry = load_registry()
    instance = {"case_id": "CASE_009", "component": "document-pipeline", "status": "success",
                "chunks": chunks, "excluded_documents": []}
    assert validate_instance(instance, "page_chunks.schema.json", schemas, registry) == []


def test_non_text_document_is_explicitly_excluded_without_a_fake_chunk(isolated_data):
    result = ct.assemble_chunks("CASE_009", [], ["DOC_010"])

    assert result == {
        "chunks": [],
        "excluded_documents": [
            {"document_id": "DOC_010", "reason": "non_text_expert_review_only"},
        ],
    }

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tools"))
    from _validation import load_registry, validate_instance

    schemas, registry = load_registry()
    instance = {"case_id": "CASE_009", "component": "document-pipeline", "status": "success", **result}
    assert validate_instance(instance, "page_chunks.schema.json", schemas, registry) == []


def test_document_cannot_be_chunked_and_excluded(isolated_data):
    _write_redacted(isolated_data, "CASE_009", "DOC_010", "<<<PAGE page=1>>>\ntext\n")
    with pytest.raises(SystemExit):
        ct.assemble_chunks("CASE_009", ["DOC_010"], ["DOC_010"])
