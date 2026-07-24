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


def _segment_doc(document_id, source_document_id="DOC_001"):
    return {
        "document_id": document_id,
        "file_name": f"{document_id}.pdf",
        "file_path": f"data/raw/CASE_030/{document_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "document_role": "segment",
        "source_document_id": source_document_id,
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
