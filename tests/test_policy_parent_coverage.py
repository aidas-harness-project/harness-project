"""Part 8 code gate: parent-level whole-page coverage.

The per-document boundary inventory only accounts for pages a segment already
owns; it cannot see a parent-PDF page that was never carved into any segment.
These tests exercise policy_completeness.check_policy_parent_coverage /
unresolved_parent_pages, which close that gap (CASE_030's 133 unowned pages).
"""
import policy_completeness as pc


FILENAME = "policy_parent_coverage_DOC_001.json"


def _manifest():
    return {
        "case_id": "CASE_030",
        "documents": [
            {"document_id": "DOC_001", "document_role": "physical",
             "document_type": "insurance_policy",
             "downstream_disposition": "automated_text_pipeline"},
            {"document_id": "DOC_005", "document_role": "segment",
             "source_document_id": "DOC_001",
             "document_type": "insurance_policy",
             "downstream_disposition": "automated_text_pipeline"},
        ],
    }


def _reference_table_for(doc_id):
    # DOC_001 has a reference table RT-aaaa; anything else has none.
    if doc_id == "DOC_001":
        return {"tables": [{"table_uid": "RT-aaaaaaaaaaaaaaaa"}]}
    return None


def _page(lp, disp, **kw):
    base = {"logical_page": lp, "physical_page": lp + 7, "disposition": disp,
            "owner_document_id": None, "table_uid": None,
            "reference_table_document_id": None, "reason": None}
    base.update(kw)
    return base


def _clean_data(total=3):
    return {
        "parent_document_id": "DOC_001",
        "total_logical_pages": total,
        "pages": [
            _page(1, "administrative_excluded", reason="표지"),
            _page(2, "owned_by_segment", owner_document_id="DOC_005"),
            _page(3, "reference_table", table_uid="RT-aaaaaaaaaaaaaaaa",
                  reference_table_document_id="DOC_001"),
        ],
    }


def test_clean_coverage_passes():
    errors = pc.check_policy_parent_coverage(
        _clean_data(), FILENAME, _manifest(), _reference_table_for)
    assert errors == [], errors


def test_coverage_gap_is_blocker():
    data = _clean_data(total=4)  # declares 4 pages but only provides 3
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("coverage gap" in e for e in errors), errors


def test_duplicate_logical_page_is_blocker():
    data = _clean_data()
    data["pages"].append(_page(2, "administrative_excluded", reason="dup"))
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("more than once" in e for e in errors), errors


def test_out_of_range_page_is_blocker():
    data = _clean_data()
    data["pages"].append(_page(9, "administrative_excluded", reason="oob"))
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("outside 1.." in e for e in errors), errors


def test_owned_by_unregistered_segment_is_blocker():
    data = _clean_data()
    data["pages"][1] = _page(2, "owned_by_segment", owner_document_id="DOC_099")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("not a registered manifest document" in e for e in errors), errors


def test_reference_table_uid_absent_is_blocker():
    data = _clean_data()
    data["pages"][2] = _page(3, "reference_table",
                             table_uid="RT-bbbbbbbbbbbbbbbb",
                             reference_table_document_id="DOC_001")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("not present in reference_table" in e for e in errors), errors


def test_reference_table_document_missing_is_blocker():
    data = _clean_data()
    data["pages"][2] = _page(3, "reference_table",
                             table_uid="RT-aaaaaaaaaaaaaaaa",
                             reference_table_document_id="DOC_007")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("no reference_table contract" in e for e in errors), errors


def test_parent_being_a_segment_is_blocker():
    manifest = _manifest()
    # make DOC_001 look like a segment
    manifest["documents"][0]["document_role"] = "segment"
    errors = pc.check_policy_parent_coverage(
        _clean_data(), FILENAME, manifest, _reference_table_for)
    assert any("is a segment" in e for e in errors), errors


def test_filename_doc_mismatch_is_blocker():
    errors = pc.check_policy_parent_coverage(
        _clean_data(), "policy_parent_coverage_DOC_002.json", _manifest(),
        _reference_table_for)
    assert any("does not match filename" in e for e in errors), errors


def test_unresolved_parent_pages_reported():
    data = _clean_data()
    data["pages"][0] = _page(1, "review_required", reason="병합셀 표 재검토 필요")
    # cross-contract check itself does not treat review_required as an error...
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert errors == [], errors
    # ...but the finalize-time unresolved check does.
    blockers = pc.unresolved_parent_pages(data)
    assert any("review_required" in b for b in blockers), blockers


def test_extraction_failed_is_unresolved():
    data = _clean_data()
    data["pages"][0] = _page(1, "extraction_failed", reason="OCR 실패")
    assert any("extraction_failed" in b for b in pc.unresolved_parent_pages(data))


def test_all_resolved_has_no_unresolved():
    assert pc.unresolved_parent_pages(_clean_data()) == []
