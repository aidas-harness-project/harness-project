"""Part 3: exact source-span inventory and policy completeness gating."""
import hashlib
import json

import dao
import policy_completeness as pc


PAGE = "제1조(지급)\n① 진단 시 지급\n② 동일 사고는 1회 한도\n"
REDACTED = f"<<<PAGE page=1>>>\n{PAGE}"


def _uid(prefix, text):
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _normalized():
    clause_uid = "PC-1111111111111111"
    return {
        "case_id": "CASE_030",
        "component": "policy-pipeline",
        "status": "success",
        "clauses": [{
            "clause_uid": clause_uid,
            "clause_id": "C-1",
            "source_boundary_uids": [
                _uid("PB", "제1조(지급)\n"),
                _uid("PB", "① 진단 시 지급\n"),
                _uid("PB", "② 동일 사고는 1회 한도\n"),
            ],
            "clause_kind": "coverage",
            "coverage_type": "진단비",
            "payout_conditions": [{"condition_uid": "CI-1111111111111111", "text": "진단 시 지급", "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "① 진단 시 지급",
            }], "support_level": "direct", "support_rationale": "원문 직접 진술",
                "review_required": False}],
            "exclusions": [],
            "reduction_conditions": [{"condition_uid": "CI-2222222222222222", "text": "동일 사고 1회", "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "② 동일 사고는 1회 한도",
            }], "support_level": "direct", "support_rationale": "원문 직접 진술",
                "review_required": False}],
            "definitions": [],
            "obligations": [],
            "claim_requirements": [],
            "termination_conditions": [],
            "dispute_resolution_conditions": [],
            "coverage_start_conditions": [],
            "reference_table_refs": [],
            "confidence": 0.9,
            "review_required": False,
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "제1조(지급)",
            }],
        }],
    }


def _inventory():
    chunks = [
        ("제1조(지급)\n", "heading", "clause", None),
        ("① 진단 시 지급\n", "paragraph", "payout_conditions", "CI-1111111111111111"),
        ("② 동일 사고는 1회 한도\n", "paragraph", "reduction_conditions", "CI-2222222222222222"),
    ]
    boundaries = []
    spans = []
    offset = 0
    for quote, level, bucket, condition_uid in chunks:
        boundary_uid = _uid("PB", quote)
        boundaries.append({
            "boundary_uid": boundary_uid,
            "boundary_level": level,
            "label": quote.strip(),
            "disposition": "normalized",
            "normalized_mappings": [{
                "clause_uid": "PC-1111111111111111",
                "display_clause_id": "C-1",
                "bucket": bucket,
                "condition_uid": condition_uid,
            }],
            "reason": None,
            "review_required": False,
        })
        spans.append({
            "span_uid": _uid("PS", quote),
            "page": 1,
            "start_char": offset,
            "end_char": offset + len(quote),
            "quote": quote,
            "disposition": "boundary",
            "boundary_uid": boundary_uid,
            "exclusion_reason": None,
        })
        offset += len(quote)
    return {
        "case_id": "CASE_030",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "boundaries": boundaries,
        "page_spans": spans,
    }


def test_complete_inventory_passes():
    errors = pc.check_policy_boundary_inventory(
        _inventory(),
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert errors == []


def test_uncovered_second_paragraph_is_rejected():
    inventory = _inventory()
    removed = inventory["page_spans"].pop()
    inventory["boundaries"] = [
        b for b in inventory["boundaries"]
        if b["boundary_uid"] != removed["boundary_uid"]
    ]
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("uncovered non-whitespace" in error for error in errors)


def test_two_numbered_paragraphs_in_one_boundary_are_rejected():
    inventory = _inventory()
    second, third = inventory["page_spans"][1:]
    merged_quote = second["quote"] + third["quote"]
    second["end_char"] = third["end_char"]
    second["quote"] = merged_quote
    inventory["page_spans"] = inventory["page_spans"][:2]
    third_uid = third["boundary_uid"]
    inventory["boundaries"] = [
        b for b in inventory["boundaries"]
        if b["boundary_uid"] != third_uid
    ]
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("spans 2 article/paragraph/item anchors" in error for error in errors)


def test_mapping_to_missing_condition_is_rejected():
    inventory = _inventory()
    inventory["boundaries"][1]["normalized_mappings"][0]["condition_uid"] = \
        "CI-9999999999999999"
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("does not resolve" in error for error in errors)


def test_normalized_source_boundary_uid_must_resolve_bidirectionally():
    normalized = _normalized()
    normalized["clauses"][0]["source_boundary_uids"][0] = \
        "PB-9999999999999999"
    errors = pc.check_policy_boundary_inventory(
        _inventory(),
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        normalized,
    )
    assert any("source_boundary_uids do not resolve" in error
               for error in errors), errors
    assert any("omits inventory boundaries" in error for error in errors), errors


def test_inventory_mapping_must_be_declared_by_clause():
    normalized = _normalized()
    normalized["clauses"][0]["source_boundary_uids"].pop()
    errors = pc.check_policy_boundary_inventory(
        _inventory(),
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        normalized,
    )
    assert any("omits inventory boundaries" in error for error in errors), errors


def test_normative_paragraph_cannot_be_hidden_in_excluded_span():
    inventory = _inventory()
    paragraph_span = inventory["page_spans"][1]
    boundary_uid = paragraph_span["boundary_uid"]
    inventory["boundaries"] = [
        boundary for boundary in inventory["boundaries"]
        if boundary["boundary_uid"] != boundary_uid
    ]
    paragraph_span.update({
        "disposition": "excluded",
        "boundary_uid": None,
        "exclusion_reason":
            "segment body text covered under its article boundary; "
            "not separately normalized",
    })
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("normative policy text" in error for error in errors), errors
    assert any("blanket body exclusion" in error for error in errors), errors


def test_review_and_extraction_boundaries_block_finalize():
    inventory = _inventory()
    inventory["boundaries"][1].update({
        "disposition": "review_required",
        "normalized_mappings": [],
        "reason": "meaning uncertain",
        "review_required": True,
    })
    assert pc.unresolved_boundaries(inventory)


def test_inventory_schema_accepts_complete_contract():
    errors = dao._schema_check(
        _inventory(), "policy_boundary_inventory.schema.json")
    assert errors == []


def test_normalized_schema_requires_condition_support_metadata():
    normalized = _normalized()
    item = normalized["clauses"][0]["payout_conditions"][0]
    del item["support_level"]
    errors = dao._schema_check(
        normalized, "normalized_policy_clause.schema.json")
    assert any("support_level" in error for error in errors)


def test_normalized_schema_composite_support_requires_two_refs_and_review():
    normalized = _normalized()
    item = normalized["clauses"][0]["payout_conditions"][0]
    item["support_level"] = "composite"
    errors = dao._schema_check(
        normalized, "normalized_policy_clause.schema.json")
    assert any("evidence_references" in error or "review_required" in error
               for error in errors)


def test_completion_gate_requires_inventory_for_each_policy_doc(
        isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    manifest = {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_030/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "cross_validation_status": "agreed",
            "redacted_text_path":
                "data/processed/CASE_030/DOC_001/redacted_text.md",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
        }],
    }
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (out / "normalized_policy_clause_DOC_001.json").write_text(
        json.dumps(_normalized(), ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    assert any("missing policy_boundary_inventory_DOC_001.json" in b for b in blockers)


def _single_page_manifest_and_norm(out):
    """A minimal DOC_001 policy set (manifest + normalized + inventory) whose
    only processed page is REDACTED (one logical page). Used by the parent-
    coverage finalize-gate tests below."""
    manifest = {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_030/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "document_role": "physical",
            "ocr_status": "completed",
            "cross_validation_status": "agreed",
            "redacted_text_path":
                "data/processed/CASE_030/DOC_001/redacted_text.md",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
        }],
    }
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (out / "normalized_policy_clause_DOC_001.json").write_text(
        json.dumps(_normalized(), ensure_ascii=False), encoding="utf-8")
    (out / "policy_boundary_inventory_DOC_001.json").write_text(
        json.dumps(_inventory(), ensure_ascii=False), encoding="utf-8")


def test_completion_gate_requires_parent_coverage_contract(isolated_dao):
    """The real CASE_030 regression: a physical policy parent with a complete
    per-document inventory but NO whole-page coverage accounting must not
    finalize -- the 133 unowned pages were invisible before this gate."""
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    _single_page_manifest_and_norm(out)

    blockers = dao._policy_completion_blockers("CASE_030")

    assert any("missing policy_parent_coverage_DOC_001.json" in b
               for b in blockers), blockers


def test_parent_coverage_gap_blocks_finalize(isolated_dao):
    """A parent-coverage contract that omits some logical pages is itself a
    blocker -- declaring total=2 but only covering page 1."""
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    _single_page_manifest_and_norm(out)
    coverage = {
        "case_id": "CASE_030", "component": "policy-pipeline",
        "status": "success", "parent_document_id": "DOC_001",
        "total_logical_pages": 2,
        "pages": [{
            "logical_page": 1, "physical_page": 8,
            "disposition": "owned_by_segment", "owner_document_id": "DOC_001",
            "table_uid": None, "reference_table_document_id": None,
            "reason": None,
        }],
    }
    (out / "policy_parent_coverage_DOC_001.json").write_text(
        json.dumps(coverage, ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    assert any("coverage gap" in b for b in blockers), blockers
    assert not any("missing policy_parent_coverage" in b for b in blockers)


def test_parent_coverage_review_required_page_blocks_finalize(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    _single_page_manifest_and_norm(out)
    coverage = {
        "case_id": "CASE_030", "component": "policy-pipeline",
        "status": "success", "parent_document_id": "DOC_001",
        "total_logical_pages": 1,
        "pages": [{
            "logical_page": 1, "physical_page": 8,
            "disposition": "review_required", "owner_document_id": None,
            "table_uid": None, "reference_table_document_id": None,
            "reason": "복잡한 병합셀 표 2차원 순서 확인 필요",
        }],
    }
    (out / "policy_parent_coverage_DOC_001.json").write_text(
        json.dumps(coverage, ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    assert any("unresolved parent page" in b and "review_required" in b
               for b in blockers), blockers


# --- Segmented-parent / reference-table-only carve-outs (CASE_030 2026-07-24) ---
#
# When a physical parent is fully carved into segments, it is normalized THROUGH
# those segments, not on its own body -- its per-document normalization/inventory/
# audit requirement is waived and its completeness is governed by parent-coverage.
# A reference-table-only segment (별표 appendix) carries structured tables, not
# clauses, so its normalized-clause requirement is waived but its boundary
# inventory is still enforced.


def _segment_doc(document_id, source_document_id="DOC_001",
                 policy_processing_role="reference_table_only"):
    return {
        "document_id": document_id,
        "file_name": f"{document_id}.pdf",
        "file_path": f"data/raw/CASE_030/{document_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "document_role": "segment",
        "source_document_id": source_document_id,
        "policy_processing_role": policy_processing_role,
        "ocr_status": "completed",
        "cross_validation_status": "agreed",
        "redacted_text_path":
            f"data/processed/CASE_030/{document_id}/redacted_text.md",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
    }


def test_segmented_parent_is_exempt_from_per_doc_normalization(isolated_dao):
    """A physical parent that owns a segment must NOT be asked for its own
    normalized clauses / inventory / audit -- only its parent-coverage matters.
    Before the carve-out this raised 'normalized clauses is empty' + 'missing
    policy_boundary_inventory_DOC_001.json' for the empty parent."""
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    parent = {
        "document_id": "DOC_001", "file_name": "DOC_001.pdf",
        "file_path": "data/raw/CASE_030/DOC_001.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "document_role": "physical",
        "policy_processing_role": "segmented_parent",
        "ocr_status": "completed", "cross_validation_status": "agreed",
        "redacted_text_path": "data/processed/CASE_030/DOC_001/redacted_text.md",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
    }
    manifest = {"case_id": "CASE_030", "documents": [parent, _segment_doc("DOC_009")]}
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # DOC_001 has an EMPTY normalized file and no inventory/audit at all.
    empty_parent = _normalized()
    empty_parent["clauses"] = []
    (out / "normalized_policy_clause_DOC_001.json").write_text(
        json.dumps(empty_parent, ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    # The parent itself raises nothing (it's exempt). Any remaining blockers must
    # be about the SEGMENT (DOC_009) or the missing parent-coverage, never the
    # empty parent's own normalization/inventory/audit.
    assert not any("DOC_001: normalized clauses is empty" in b for b in blockers), blockers
    assert not any("DOC_001: missing policy_boundary_inventory" in b for b in blockers), blockers
    assert not any("DOC_001: missing policy_audit_result" in b for b in blockers), blockers


def test_reference_table_only_segment_is_exempt_from_clauses_but_needs_inventory(
        isolated_dao):
    """A segment whose content is a reference_table (no normalized clauses) is
    waived the normalized-clause requirement, but its boundary inventory is
    still enforced -- so a MISSING inventory is still a blocker, while a missing
    normalized_policy_clause contract is not."""
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_007"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    manifest = {"case_id": "CASE_030", "documents": [_segment_doc("DOC_007")]}
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # A reference_table contract exists; NO normalized_policy_clause_DOC_007.json.
    reference_table = {
        "case_id": "CASE_030", "component": "policy-pipeline", "status": "success",
        "source_document_id": "DOC_007",
        "tables": [{
            "table_uid": "RT-1111111111111111", "table_id": "T-1",
            "title": "장해분류표", "columns": [{"column_key": "c", "label": "분류"}],
            "rows": [{"row_uid": "RR-1111111111111111", "cells": [{
                "cell_uid": "RC-1111111111111111", "column_key": "c",
                "value": "제1조(지급)",
                "evidence_references": [{"document_id": "DOC_007", "page": 1, "quote": "제1조(지급)"}],
                "review_required": True,
            }]}],
            "evidence_references": [{"document_id": "DOC_007", "page": 1, "quote": "제1조(지급)"}],
            "review_required": True,
        }],
    }
    (out / "reference_table_DOC_007.json").write_text(
        json.dumps(reference_table, ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    # No complaint about a missing normalized clause contract...
    assert not any("missing normalized_policy_clause_DOC_007" in b for b in blockers), blockers
    # ...but the missing boundary inventory is still enforced for the segment.
    assert any("DOC_007: missing policy_boundary_inventory_DOC_007.json" in b
               for b in blockers), blockers


def test_reference_table_only_segment_cannot_finalize_with_review_flags(
        isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_007"
    out.mkdir(parents=True)
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(REDACTED, encoding="utf-8")
    manifest = {"case_id": "CASE_030", "documents": [_segment_doc("DOC_007")]}
    (out / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    reference_table = {
        "case_id": "CASE_030", "component": "policy-pipeline",
        "status": "success", "source_document_id": "DOC_007",
        "tables": [{
            "table_uid": "RT-1111111111111111", "table_id": "T-1",
            "title": "제1조(지급)",
            "columns": [{"column_key": "c", "label": "분류"}],
            "rows": [{"row_uid": "RR-1111111111111111", "cells": [{
                "cell_uid": "RC-1111111111111111", "column_key": "c",
                "value": "제1조(지급)",
                "evidence_references": [{
                    "document_id": "DOC_007", "page": 1,
                    "quote": "제1조(지급)",
                }],
                "review_required": True,
            }]}],
            "evidence_references": [{
                "document_id": "DOC_007", "page": 1,
                "quote": "제1조(지급)",
            }],
            "review_required": True,
        }],
    }
    (out / "reference_table_DOC_007.json").write_text(
        json.dumps(reference_table, ensure_ascii=False), encoding="utf-8")
    inventory = _inventory()
    inventory["source_document_id"] = "DOC_007"
    for boundary in inventory["boundaries"]:
        boundary.update({
            "disposition": "excluded_with_reason",
            "normalized_mappings": [],
            "reason": "structured table represented in reference table",
            "review_required": False,
        })
    (out / "policy_boundary_inventory_DOC_007.json").write_text(
        json.dumps(inventory, ensure_ascii=False), encoding="utf-8")

    blockers = dao._policy_completion_blockers("CASE_030")

    assert any("unresolved reference table" in blocker
               and "review_required=true" in blocker
               for blocker in blockers), blockers


# --------------------------------------------------------------------------
# Part 11C: structural anchors, heading-only boundaries, evidence binding.
# --------------------------------------------------------------------------

def _excluded_page(page_body, reason="목차/표지"):
    """An inventory whose single excluded span covers the whole page."""
    return {
        "case_id": "CASE_030",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "boundaries": [],
        "page_spans": [{
            "span_uid": _uid("PS", page_body),
            "page": 1,
            "start_char": 0,
            "end_char": len(page_body),
            "quote": page_body,
            "disposition": "excluded",
            "boundary_uid": None,
            "exclusion_reason": reason,
        }],
    }


def _anchor_errors(page_body):
    return pc.check_policy_boundary_inventory(
        _excluded_page(page_body),
        "policy_boundary_inventory_DOC_001.json",
        f"<<<PAGE page=1>>>\n{page_body}",
        None,
    )


def test_korean_item_marker_hidden_in_excluded_span_is_rejected():
    """'가. 회사는 보험금을 지급하지 않습니다' is normative -- it may not be
    buried in an excluded span."""
    errors = _anchor_errors("가. 회사는 보험금을 지급하지 않습니다\n")
    assert any("anchor" in e and "excluded source span" in e
               for e in errors), errors


def test_parenthesised_and_bare_number_item_markers_are_recognised():
    for body in ("(1) 회사는 보상합니다\n",
                 "1) 회사는 보상합니다\n",
                 "(가) 회사는 보상합니다\n"):
        errors = _anchor_errors(body)
        assert any("anchor" in e and "excluded source span" in e
                   for e in errors), (body, errors)


def test_ordinary_prose_is_not_a_false_positive_anchor():
    """A sentence-ending syllable + period must not read as an item marker."""
    errors = _anchor_errors("회사는 보험금을 지급합니다. 다만 예외가 있습니다\n")
    assert not any("anchor" in e for e in errors), errors


def test_heading_only_boundary_cannot_carry_a_condition():
    """A title span mapped to a condition bucket is rejected; the body needs
    its own boundary span."""
    inventory = _inventory()
    # Re-point the heading boundary's mapping from 'clause' to a condition.
    inventory["boundaries"][0]["normalized_mappings"][0].update({
        "bucket": "payout_conditions",
        "condition_uid": "CI-1111111111111111",
    })
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("heading only" in e and "payout_conditions" in e
               for e in errors), errors


def test_heading_boundary_mapped_to_clause_identity_still_passes():
    """The legitimate pattern: heading -> clause, body -> conditions."""
    errors = pc.check_policy_boundary_inventory(
        _inventory(),
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert not any("heading only" in e for e in errors), errors


def test_clause_evidence_outside_its_declared_boundaries_is_rejected():
    """A clause may not cite text physically located in another boundary."""
    normalized = _normalized()
    clause = normalized["clauses"][0]
    # Declare only the heading + first paragraph, but keep citing the second.
    clause["source_boundary_uids"] = [
        _uid("PB", "제1조(지급)\n"),
        _uid("PB", "① 진단 시 지급\n"),
    ]
    errors = pc.check_clause_evidence_within_boundaries(
        normalized, _inventory(), REDACTED)
    assert any("does not fall within" in e and "reduction_conditions" in e
               for e in errors), errors


def test_clause_evidence_inside_declared_boundaries_passes():
    errors = pc.check_clause_evidence_within_boundaries(
        _normalized(), _inventory(), REDACTED)
    assert errors == [], errors


def test_evidence_landing_in_an_excluded_span_is_rejected():
    """Excluded (non-normative) source text may not substantiate a condition."""
    inventory = _inventory()
    # Turn the second paragraph's span into an excluded one, while the clause
    # keeps citing it.
    span = inventory["page_spans"][2]
    excluded_uid = span["boundary_uid"]
    span.update({
        "disposition": "excluded",
        "boundary_uid": None,
        "exclusion_reason": "판단상 비규범 텍스트",
    })
    inventory["boundaries"] = [
        b for b in inventory["boundaries"]
        if b["boundary_uid"] != excluded_uid
    ]
    normalized = _normalized()
    normalized["clauses"][0]["source_boundary_uids"] = [
        _uid("PB", "제1조(지급)\n"),
        _uid("PB", "① 진단 시 지급\n"),
    ]
    errors = pc.check_clause_evidence_within_boundaries(
        normalized, inventory, REDACTED)
    assert any("overlaps a span the inventory marked excluded" in e
               for e in errors), errors


# ---------------------------------------------------------------------------
# Operative-predicate coverage (CASE_112). The gate uses this regex to decide
# whether a page carries normative content and therefore may NOT be dismissed
# as administrative. Every string below is real CASE_112 policy wording that the
# pre-2026-08 pattern missed, which let genuine payout/exclusion clauses look
# non-normative.
# ---------------------------------------------------------------------------

def test_honorific_payout_forms_are_operative():
    """"보상하여 드립니다" states the same rule as "보상합니다" (DOC_052 제1조)."""
    for text in [
        "회사는 ... 수탁물이 화재로 입은 손해만을 보상하여 드립니다.",
        "그러나 아래의 경우에는 보상하여 드립니다.",
        "회사는 보험금을 지급하여 드립니다.",
    ]:
        assert pc._OPERATIVE_PREDICATE_RE.search(text), text


def test_waiver_and_negated_application_are_operative():
    """대위권포기 특별약관's entire operative content is "…포기합니다" (DOC_089/192)."""
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "회사는 보통약관 제14조(대위권)의 규정에도 불구하고 "
        "아래에 기재된 사람에 대한 대위권을 포기합니다."
    )
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "단, 보호자의 감독하의 비행에 대해서는 적용하지 아니합니다."
    )


def test_liability_assumption_and_substitution_are_operative():
    # DOC_078/DOC_184 제1조, DOC_179 제5조(현물보상).
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "아래에 기재된 보트로 생긴 배상책임을 부담하기로 정합니다."
    )
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "회사는 현물보상으로서 보험금의 지급에 갈음할 수 있습니다."
    )


def test_deemed_effect_provision_is_operative():
    # DOC_103/DOC_205: a deeming rule, not boilerplate.
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "피보험자가 소속단체를 탈퇴하는 즉시 당해 피보험자의 계약은 해지된 것으로 합니다."
    )


def test_negated_grant_and_affirmative_duty_are_operative():
    # DOC_104 제재위반 부담보 (no 보상/지급 verb at all); DOC_101 제6조.
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "보험회사는 아래의 제재에 반하는 위험의 보장, 보험금의 지급 또는 "
        "이익의 제공을 하지 않습니다."
    )
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "회사는 보험계약자에게 보험증권을 드려야 하고, 그 약관의 주요한 내용을 알려드립니다."
    )
    assert pc._OPERATIVE_PREDICATE_RE.search(
        "개별 피보험자에게는 가입증명서를 발급하여 드립니다."
    )


def test_boilerplate_and_qualifiers_stay_non_operative():
    """준용규정 / 목차 wording must NOT count as normative content.

    This is the half of the contract that keeps the administrative-exclusion
    path usable: a 준용규정-only page (DOC_087/DOC_094) states no rule of its
    own, and 포함합니다/따릅니다/적용합니다 merely qualify a rule stated
    elsewhere.
    """
    for text in [
        "이 특별약관에 정하지 않은 사항은 보통약관을 따릅니다.",
        "제4회 :    년  월  일(총 보험료의 20% 해당액)",
        "의사(한의사 및 수의사를 포함합니다), 간호사, 약사",
        "이 규정을 적용합니다.",
    ]:
        assert not pc._OPERATIVE_PREDICATE_RE.search(text), text


# --- 제N조 cross-references are not nested headings (CASE_907) ---------------

def test_article_cross_reference_is_not_a_second_anchor():
    """Korean policy text cites other articles mid-sentence constantly. Before
    2026-08-03 `_STRUCTURAL_ANCHOR_RE` matched `제N조` anywhere, so one article
    with one citing sentence read as 2 anchors and `check_policy_boundary_
    inventory` refused the span with "split material subparagraphs" -- 112 of
    CASE_907/DOC_003's 1321 spans, 38 of them unsplittable (both anchors in one
    sentence). Line-anchoring makes 제N조 consistent with the item forms, which
    were already `^`-anchored for the same class of false positive."""
    span = ("제1조(보상하는 손해) \n회사는 특별약관 제2조(보상하지 않는 손해)의 "
            "규정에도 불구하고 보상합니다.")
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall(span)) == 1


def test_cross_reference_inside_a_paragraph_item_is_not_an_anchor():
    """The unsplittable shape: a circled-paragraph item citing another article.
    One anchor (①), not two."""
    span = "① 회사는 보통약관 제8조(보험금 등의 지급한도) 제1항을 아래의 사항으로 대체합니다."
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall(span)) == 1


def test_two_real_article_headings_still_count_as_two():
    """The rule must keep doing its actual job: a span swallowing two article
    headings is still refused."""
    span = "제1조(보상하는 손해)\n내용입니다.\n제2조(보상하지 않는 손해)\n다른 내용입니다."
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall(span)) == 2


def test_indented_article_heading_is_still_an_anchor():
    """Leading whitespace is layout, not a demotion from heading."""
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall("  제3조(보험기간의 연장)\n내용")) == 1


def test_sentence_referring_to_paragraph_items_is_not_an_anchor():
    """DOC_003 p11: a sentence that REFERS to two paragraph items. Before the
    circled forms were line-anchored this was the last surviving anchor error
    after the 제N조 fix -- 3 anchors for a span holding one real item."""
    span = ("② 대여자동차로 대체하여 사용할 수 없는 차종은 실임차료.\n"
            "* 위 ①, ② 조항은 자동차 보험 표준약관이 변경되는 경우 그 변경사항도 포함합니다.")
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall(span)) == 1


def test_line_initial_paragraph_items_still_count():
    """Real items must keep matching: two line-initial circled markers are two
    anchors."""
    span = "① 첫 번째 항목입니다.\n② 두 번째 항목입니다."
    assert len(pc._STRUCTURAL_ANCHOR_RE.findall(span)) == 2
