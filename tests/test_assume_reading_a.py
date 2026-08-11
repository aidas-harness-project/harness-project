"""--on-disagreement assume-reading-a: continue past P8, record it honestly.

The operator's standing instruction is that in real practice a human
adjudicates a P8 disagreement, so a PoC throughput run should take reading_a
and keep going rather than halt. That instruction existed only as prose and was
lost between sessions -- on CASE_911/DOC_005 the pipeline spent 14 minutes on a
19-page document resolving 8 disagreements page by page. This flag puts the
policy in code.

The whole contract is that it DEFERS the judgement without disguising it: the
page never reads `agreed`, the document never reads clean, and review_required
stays true.
"""
import run_checkpoint1


def _page(num, agreement, reading_a="A text", reading_b="B text"):
    return {
        "page": num,
        "agreement": agreement,
        "reading_a": reading_a,
        "reading_b": reading_b,
        "disagreement_details": [] if agreement == "agreed" else ["date differs"],
    }


def _ocr_data(pages):
    return {
        "pages": pages,
        "providers": {"reader_a": None, "reader_b": None, "comparator": None},
    }


def test_disagreed_pages_become_assume_reading_a_and_agreed_pages_are_untouched():
    data = _ocr_data([_page(1, "agreed"), _page(2, "disagreed"),
                      _page(3, "disagreed")])

    resolved = run_checkpoint1._apply_assume_reading_a(data)

    assert resolved == [2, 3]
    assert data["pages"][0]["agreement"] == "agreed"
    assert "auto_resolution" not in data["pages"][0]
    for p in data["pages"][1:]:
        assert p["agreement"] == "assume_reading_a"
        auto = p["auto_resolution"]
        assert auto["policy"] == "assume_reading_a"
        assert auto["chosen_reading"] == "reading_a"
        assert auto["resolved_at"]


def test_a_disagreement_is_never_relabelled_agreed():
    """The readers did not agree. A policy default must not read as concurrence."""
    data = _ocr_data([_page(1, "disagreed")])
    run_checkpoint1._apply_assume_reading_a(data)
    assert data["pages"][0]["agreement"] != "agreed"


def test_assembled_contract_reports_the_policy_and_still_demands_review():
    data = _ocr_data([_page(1, "agreed"), _page(2, "disagreed")])
    run_checkpoint1._apply_assume_reading_a(data)

    result = run_checkpoint1._assemble_ocr_result(
        "CASE_911", "DOC_005", "RUN_20260811_006", data, source_total_pages=2)

    assert result["cross_validation_status"] == "assume_reading_a_unreviewed"
    assert result["review_required"] is True
    assert result["ocr_quality"] == "low"
    assert "2" in result["review_reason"]
    # The auto-taken page has real text on disk, so it carries a text_path --
    # that is what lets the run continue at all.
    assert result["pages"][1]["text_path"] is not None
    assert result["pages"][1]["cross_validation"]["agreement"] == "assume_reading_a"
    assert result["pages"][1]["cross_validation"]["auto_resolution"][
        "chosen_reading"] == "reading_a"
    # The original disagreement evidence survives for the later human pass.
    assert result["pages"][1]["cross_validation"]["disagreement_details"]


def test_without_the_flag_a_disagreement_still_blocks():
    """Default behaviour is unchanged: P8 remains a hard gate."""
    data = _ocr_data([_page(1, "agreed"), _page(2, "disagreed")])
    # No _apply_assume_reading_a call -- this is the `block` path.
    result = run_checkpoint1._assemble_ocr_result(
        "CASE_911", "DOC_005", "RUN_20260811_006", data, source_total_pages=2)

    assert result["cross_validation_status"] == "disagreed_pending_review"
    assert result["review_required"] is True
    assert result["pages"][1]["text_path"] is None


def test_a_fully_agreeing_document_is_unaffected_by_the_policy():
    data = _ocr_data([_page(1, "agreed"), _page(2, "agreed")])
    assert run_checkpoint1._apply_assume_reading_a(data) == []

    result = run_checkpoint1._assemble_ocr_result(
        "CASE_911", "DOC_005", "RUN_20260811_006", data, source_total_pages=2)
    assert result["cross_validation_status"] == "agreed"
    assert result["review_required"] is False
    assert result["ocr_quality"] == "high"


def test_every_page_claiming_a_text_path_is_a_page_that_gets_written(monkeypatch):
    """The contract's text_path set and the written-page set must be identical.

    Real bug, found by running the flag on CASE_911/DOC_005: the page-write loop
    was gated on `agreement == "agreed"` while _assemble_ocr_result stamped a
    text_path for `assume_reading_a` too, so the contract advertised 19 pages of
    text with only 11 on disk -- 8 dangling paths into pages 9-16. The schema
    rejection on an unrelated enum is the only thing that stopped a document
    looking complete while pointing at files that do not exist.
    """
    import inspect
    import re

    data = _ocr_data([_page(1, "agreed"), _page(2, "disagreed"),
                      _page(3, "disagreed"), _page(4, "agreed")])
    run_checkpoint1._apply_assume_reading_a(data)

    # Read the guard out of the production source rather than restating it --
    # a test that reimplements the loop passes no matter what the loop does,
    # which is exactly how the first version of this test missed the bug.
    src = inspect.getsource(run_checkpoint1.run_checkpoint1)
    guard = re.search(
        r'for p in ocr_data\["pages"\]:\s*\n(?:\s*#.*\n)*\s*if (.+?):\s*\n\s*_write_page_text',
        src)
    assert guard, "page-write loop not found -- update this test to match"

    written = {p["page"] for p in data["pages"] if eval(guard.group(1), {}, {"p": p})}
    result = run_checkpoint1._assemble_ocr_result(
        "CASE_911", "DOC_005", "RUN_20260811_010", data, source_total_pages=4)
    claimed = {p["page"] for p in result["pages"] if p["text_path"]}

    assert claimed == written == {1, 2, 3, 4}, (
        f"contract claims text for {sorted(claimed)} but the write loop writes "
        f"{sorted(written)} -- dangling text_path(s): {sorted(claimed - written)}")


def test_contract_validates_against_the_real_schema():
    """Rule 3 must accept the honest record and reject a disguised one."""
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()

    data = _ocr_data([_page(1, "disagreed")])
    run_checkpoint1._apply_assume_reading_a(data)
    result = run_checkpoint1._assemble_ocr_result(
        "CASE_911", "DOC_005", "RUN_20260811_006", data, source_total_pages=1)

    assert not validate_instance(result, "ocr_result.schema.json", schemas, registry)

    disguised = dict(result, cross_validation_status="agreed", review_required=False)
    assert validate_instance(disguised, "ocr_result.schema.json", schemas, registry), \
        "an assume_reading_a page under an 'agreed' document must be rejected"
