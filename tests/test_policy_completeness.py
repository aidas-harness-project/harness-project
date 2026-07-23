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
    return {
        "case_id": "CASE_030",
        "component": "policy-pipeline",
        "status": "success",
        "clauses": [{
            "clause_id": "C-1",
            "coverage_type": "진단비",
            "payout_conditions": [{"text": "진단 시 지급", "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "① 진단 시 지급",
            }]}],
            "exclusions": [],
            "reduction_conditions": [{"text": "동일 사고 1회", "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "② 동일 사고는 1회 한도",
            }]}],
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
        ("① 진단 시 지급\n", "paragraph", "payout_conditions", 0),
        ("② 동일 사고는 1회 한도\n", "paragraph", "reduction_conditions", 0),
    ]
    boundaries = []
    spans = []
    offset = 0
    for quote, level, bucket, index in chunks:
        boundary_uid = _uid("PB", quote)
        boundaries.append({
            "boundary_uid": boundary_uid,
            "boundary_level": level,
            "label": quote.strip(),
            "disposition": "normalized",
            "normalized_mappings": [{
                "clause_id": "C-1",
                "bucket": bucket,
                "condition_index": index,
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
    inventory["boundaries"][1]["normalized_mappings"][0]["condition_index"] = 9
    errors = pc.check_policy_boundary_inventory(
        inventory,
        "policy_boundary_inventory_DOC_001.json",
        REDACTED,
        _normalized(),
    )
    assert any("does not resolve" in error for error in errors)


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

