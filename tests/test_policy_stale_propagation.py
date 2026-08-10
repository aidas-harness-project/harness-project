"""Part 11F: finalize re-validates against the CURRENT source, and an upstream
change invalidates the downstream stages derived from it.

The attack this closes:

  1. write a normalized clause contract whose evidence matches the source
  2. rewrite redacted_text.md
  3. refresh only the inventory and the audit
  4. finalize -- which re-validated the normalized contract's SHAPE but never
     re-checked its quotes against the source, so stale evidence passed

Every boundary offset and evidence quote is expressed against exact processed
bytes, so those bytes are part of what the audit is bound to, and a change to
them must invalidate work derived from them.
"""
import json

import dao
import stage_dependencies


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


SOURCE_V1 = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급) 회사는 피보험자가 사망한 경우 사망보험금을 지급합니다.\n"
)
# Same clause, materially different text: the v1 quote no longer appears.
SOURCE_V2 = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급) 회사는 피보험자가 장해를 입은 경우 장해보험금을 지급합니다.\n"
)


def _seed(isolated_dao):
    """A minimal but real CASE_030: manifest + processed text + a normalized
    contract whose evidence genuinely matches SOURCE_V1."""
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_005"
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(SOURCE_V1, encoding="utf-8")

    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_005",
            "file_name": "DOC_005.pdf",
            "file_path": "data/raw/CASE_030/DOC_005.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
        }],
    })
    _write(out / "normalized_policy_clause_DOC_005.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "clauses": [{
            "clause_uid": "PC-1111111111111111",
            "clause_id": "C-1",
            "source_boundary_uids": ["PB-1111111111111111"],
            "clause_kind": "coverage",
            "coverage_type": "사망",
            "payout_conditions": [{
                "condition_uid": "CI-1111111111111111",
                "text": "피보험자가 사망한 경우 사망보험금을 지급",
                "evidence_references": [{
                    "document_id": "DOC_005", "page": 1,
                    "quote": "회사는 피보험자가 사망한 경우 사망보험금을 지급합니다",
                }],
                "support_level": "direct",
                "support_rationale": "원문 직접 진술",
                "review_required": False,
            }],
            "exclusions": [], "reduction_conditions": [], "definitions": [],
            "obligations": [], "claim_requirements": [],
            "termination_conditions": [], "dispute_resolution_conditions": [],
            "coverage_start_conditions": [], "reference_table_refs": [],
            "confidence": 0.9, "review_required": False,
            "evidence_references": [{
                "document_id": "DOC_005", "page": 1,
                "quote": "제3조(보험금의 지급)",
            }],
        }],
    })
    return out, processed


# --- finalize re-runs the full source check, not just the schema ----------

def test_finalize_rechecks_normalized_evidence_against_current_source(
        isolated_dao):
    """Rewriting the source after the contract was written must be caught."""
    out, processed = _seed(isolated_dao)
    before = [b for b in dao._policy_completion_blockers("CASE_030")
              if "normalized source" in b]
    assert before == [], before

    (processed / "redacted_text.md").write_text(SOURCE_V2, encoding="utf-8")

    after = [b for b in dao._policy_completion_blockers("CASE_030")
             if "normalized source" in b]
    assert any("quote not found on page 1" in b for b in after), after


def test_refreshing_only_the_inventory_does_not_hide_stale_evidence(
        isolated_dao):
    """Touching the inventory/audit cannot launder evidence that no longer
    matches the source -- the normalized contract is re-checked directly."""
    out, processed = _seed(isolated_dao)
    (processed / "redacted_text.md").write_text(SOURCE_V2, encoding="utf-8")
    # A brand-new inventory written against the NEW text changes nothing about
    # the normalized contract's own stale quotes.
    _write(out / "policy_boundary_inventory_DOC_005.json", {
        "case_id": "CASE_030", "component": "policy-pipeline",
        "status": "success", "source_document_id": "DOC_005",
        "boundaries": [], "page_spans": [],
    })
    blockers = dao._policy_completion_blockers("CASE_030")
    assert any("normalized source" in b and "quote not found" in b
               for b in blockers), blockers


# --- the audit is bound to its sources, not only to the contracts ---------

def test_audit_binding_includes_source_text_and_manifest_and_coverage(
        isolated_dao):
    _seed(isolated_dao)
    hashes, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_005")
    assert hashes["source_text_sha256"] is not None
    assert hashes["manifest_entry_sha256"] is not None
    # No parent coverage exists for this standalone document.
    assert hashes["parent_coverage_sha256"] is None


def test_changing_processed_text_makes_the_audit_binding_stale(isolated_dao):
    _, processed = _seed(isolated_dao)
    before, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_005")
    (processed / "redacted_text.md").write_text(SOURCE_V2, encoding="utf-8")
    after, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_005")
    assert before["source_text_sha256"] != after["source_text_sha256"]


def test_manifest_entry_digest_covers_the_whole_entry(
        isolated_dao, make_args):
    """The audit's manifest binding hashes the entire manifest entry -- page_map
    included -- so any edit to it makes the audit stale.

    Note the deliberate asymmetry with the invalidation cascade: the DIGEST
    moves for any field (it is a binding, and re-auditing after any manifest
    edit is the safe default), while the run-state CASCADE fires only for
    provenance-bearing fields (see
    test_provenance_manifest_change_invalidates_but_metadata_does_not).
    """
    _seed(isolated_dao)
    before, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_005")
    ok, message = dao.patch_manifest_document(
        "CASE_030", "DOC_005",
        {"ocr_quality": "high"},
        "test-agent", "RUN_20260724_001")
    assert ok, message
    after, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_005")
    assert after["manifest_entry_sha256"] != before["manifest_entry_sha256"]


# --- upstream change cascades to downstream stages ------------------------

def _run_state_with_passed(out, stages):
    _write(out / "_run_state.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "created_at": "2026-07-24T10:00:00+09:00",
        "updated_at": "2026-07-24T10:00:00+09:00",
        "stages": [
            {"stage_name": name, "status": "passed", "attempt_count": 1,
             "backup_path": f"outputs/CASE_030/_backups/step_01_{name}"}
            for name in stages
        ],
        "human_input_status": [],
    })


def test_rewriting_redacted_text_invalidates_downstream_passed_stages(
        isolated_dao, make_args):
    out, processed = _seed(isolated_dao)
    _run_state_with_passed(
        out, ["document_processing", "policy_clause_processing",
              "claim_analysis"])

    new_text = isolated_dao / "new.md"
    new_text.write_text(SOURCE_V2, encoding="utf-8")
    rc = dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(new_text),
        held_by="document-pipeline", run_id="RUN_20260724_001"))
    assert rc == 0

    state = dao.load_run_state("CASE_030")
    by_name = {s["stage_name"]: s for s in state["stages"]}
    # The stage doing the writing is untouched; everything derived from it is
    # invalidated with a reason, and keeps its historical snapshot path.
    assert by_name["document_processing"]["status"] == "passed"
    assert by_name["policy_clause_processing"]["status"] == "failed"
    # Part 11J commit a renamed this: the write became a source-text REVISION
    # (new revision file, invalidate, then flip the pointer) rather than an
    # in-place rewrite, and the recorded reason names the revision.
    assert "source text revised" in \
        by_name["policy_clause_processing"]["invalidation_reason"]
    assert by_name["policy_clause_processing"]["backup_path"] is not None
    assert by_name["claim_analysis"]["status"] == "failed"


def test_identical_rewrite_does_not_invalidate_anything(
        isolated_dao, make_args):
    """Only a real change cascades -- rewriting the same bytes is a no-op."""
    out, processed = _seed(isolated_dao)
    _run_state_with_passed(
        out, ["document_processing", "policy_clause_processing"])
    same = isolated_dao / "same.md"
    same.write_text(SOURCE_V1, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(same),
        held_by="document-pipeline", run_id="RUN_20260724_001")) == 0
    state = dao.load_run_state("CASE_030")
    by_name = {s["stage_name"]: s for s in state["stages"]}
    assert by_name["policy_clause_processing"]["status"] == "passed"


def test_provenance_manifest_change_invalidates_but_metadata_does_not(
        isolated_dao):
    out, _ = _seed(isolated_dao)
    _run_state_with_passed(
        out, ["document_processing", "policy_clause_processing"])

    # Descriptive metadata: no cascade.
    dao.patch_manifest_document(
        "CASE_030", "DOC_005", {"ocr_quality": "high"},
        "test-agent", "RUN_20260724_001")
    state = dao.load_run_state("CASE_030")
    assert next(s for s in state["stages"]
                if s["stage_name"] == "policy_clause_processing")[
                    "status"] == "passed"

    # Provenance-bearing field: cascade.
    dao.patch_manifest_document(
        "CASE_030", "DOC_005", {"source_total_pages": 240},
        "test-agent", "RUN_20260724_001")
    state = dao.load_run_state("CASE_030")
    entry = next(s for s in state["stages"]
                 if s["stage_name"] == "policy_clause_processing")
    assert entry["status"] == "failed"
    assert "source_total_pages" in entry["invalidation_reason"]


def test_dependents_of_is_transitive():
    dependents = stage_dependencies.dependents_of("document_processing")
    assert "policy_clause_processing" in dependents
    assert "claim_analysis" in dependents        # transitive
    assert "screening_report" in dependents      # transitive
    assert "document_processing" not in dependents
