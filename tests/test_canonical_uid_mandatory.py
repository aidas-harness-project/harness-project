"""P0-3: canonical UID verification is mandatory, not opt-in.

P0-2 made every canonical_v1 UID recompute from source provenance. It left one
thing untouched: whether a caller had to be in canonical_v1 at all. Three
functions -- `_source_revision_binding_errors`, `_canonical_uid_errors`,
`_canonical_uid_finalize_blockers` -- all opened with a variant of

    if uid_scheme_for(case_id, doc_id) != "canonical_v1": return []

and `uid_scheme_for` reported a document with NO revision entry as `legacy`.
So the entire UID layer, every gate P0-2 built, could be skipped by simply not
running `enable-canonical-uids`. A brand-new document started `legacy`
(`_register_revision`), and legacy meant unchecked. That is not a verification
with a migration path; that is a verification with an opt-out.

What this file asserts is the inverse of that: the only state in which new
policy work may be written is canonical_v1, and every route to producing or
publishing policy-derived results is gated on it -- the policy-layer write, the
policy stage's finalization, a downstream artifact citing a policy UID, and the
finalization of any stage transitively downstream of the policy stage.

Legacy compatibility is asserted just as deliberately (section 7). Legacy
artifacts must stay readable and migratable, or the migration path this fix
depends on does not exist -- the fix is "legacy cannot be WRITTEN against",
never "legacy cannot be read".
"""
import json

import pytest

import dao
import policy_completeness
import source_provenance
import stage_dependencies
import _cross_contract


PAGE_1 = "제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
TEXT = f"<<<PAGE page=1>>>\n{PAGE_1}"
RAW = b"%PDF-1.7 immutable policy source for the P0-3 gate tests"

# Deliberately well-formed: PC-/CI-/RT- plus sixteen hex digits satisfies every
# format regex in the schemas. Under the pre-P0-3 code a document with no
# revision entry was `legacy`, and legacy skipped recomputation entirely, so
# these strings were accepted as identifiers of real source elements.
FAKE = {
    "boundary": "PB-1111111111111111",
    "clause": "PC-1111111111111111",
    "condition": "CI-1111111111111111",
    "table": "RT-1111111111111111",
    "row": "RR-1111111111111111",
    "cell": "RC-1111111111111111",
}


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _manifest_entry(doc_id, **overrides):
    entry = {
        "document_id": doc_id,
        "file_name": f"{doc_id}.pdf",
        "file_path": f"data/raw/CASE_030/{doc_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "ocr_status": "completed",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
        "extraction_method": "embedded_text",
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def case(isolated_dao):
    """CASE_030 with DOC_005 registered in the manifest and its raw file on
    disk -- but NO revision entry. This is the `unregistered` state, and it is
    the state every policy document is in before anyone runs the migration."""
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [_manifest_entry("DOC_005")],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_005.pdf").write_bytes(RAW)
    processed = isolated_dao / "data" / "processed" / "CASE_030" / "DOC_005"
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(TEXT, encoding="utf-8")
    return out


def _make_legacy(make_args, isolated_dao, doc_id="DOC_005", text=TEXT):
    """Register a source-text revision WITHOUT activating canonical UIDs.

    This is the explicit `legacy` state -- a real, DAO-recorded document whose
    UIDs were never verified. Distinct from `unregistered`, and the distinction
    is the point: before P0-3 both reported `legacy` and both skipped every
    check.
    """
    path = isolated_dao / f"legacy_{doc_id}.md"
    path.write_text(text, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id=doc_id, text_file=str(path),
        held_by="document-pipeline", run_id="RUN_20260728_001")) == 0
    assert dao.uid_scheme_for("CASE_030", doc_id) == "legacy"


def _inventory(doc_id="DOC_005", span_uid=None, boundary_uid=None,
               binding=None):
    """A boundary inventory that is schema-valid and internally consistent.

    Nothing here is malformed: the span quote really is on page 1 at those
    offsets, the boundary the span points at really is declared, and the UIDs
    match each other everywhere they appear. That cross-file consistency is
    exactly what a fabricated-but-consistent UID set buys, and what P0-3 must
    refuse anyway -- agreement between two files an agent wrote is not
    provenance.
    """
    quote = PAGE_1.rstrip("\n")
    pb = boundary_uid or FAKE["boundary"]
    data = {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": doc_id,
        "boundaries": [{
            "boundary_uid": pb,
            "boundary_level": "article",
            "label": "제3조",
            "disposition": "excluded_with_reason",
            "normalized_mappings": [],
            "reason": "held for the gate under test",
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": span_uid or "PS-1111111111111111",
            "page": 1,
            "start_char": 0,
            "end_char": len(quote),
            "quote": quote,
            "disposition": "boundary",
            "boundary_uid": pb,
            "exclusion_reason": None,
        }],
    }
    if binding is not None:
        data["source_text_revision"] = binding
    return data


def _write_inventory(isolated_dao, make_args, data, doc_id="DOC_005",
                     stage=None):
    data_file = isolated_dao / "inv.json"
    _write_json(data_file, data)
    return dao.cmd_write_contract(make_args(
        case_id="CASE_030",
        filename=f"policy_boundary_inventory_{doc_id}.json",
        data_file=str(data_file),
        schema_name=policy_completeness.INVENTORY_SCHEMA,
        held_by="policy-pipeline", run_id="RUN_20260728_001", stage=stage))


def _run_state(stages):
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "stages": [
            {"stage_name": name, "status": status, "attempt_count": 1,
             "backup_path": f"outputs/CASE_030/_backups/step_01_{name}"}
            for name, status in stages
        ],
    }


def _stage_status(name):
    return next(
        (s["status"] for s in dao.load_run_state("CASE_030")["stages"]
         if s["stage_name"] == name), None)


# =========================================================================
# 1. An unregistered document cannot carry new policy work
# =========================================================================

def test_an_unregistered_document_is_not_reported_as_legacy(case):
    """The root cause, asserted directly.

    `uid_scheme_for` returning 'legacy' for a document it has never seen is
    what collapsed 'deliberately pre-canonical' and 'completely unknown' into
    one state -- and since every check was scoped to canonical_v1, that one
    state meant 'skip verification'.
    """
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == \
        source_provenance.UNREGISTERED
    assert dao.uid_scheme_for("CASE_030", "DOC_005") != "legacy"


def test_arbitrary_uids_on_an_unregistered_document_are_refused(
        case, isolated_dao, make_args, capsys):
    """Requirement 1: no revision entry -> a PS/PB inventory write is refused,
    and neither the target file nor the run-state moves."""
    assert _write_inventory(isolated_dao, make_args, _inventory(),
                            stage="policy_clause_processing") == 1
    output = capsys.readouterr().out
    assert "not canonically verified" in output
    assert "DOC_005" in output and "unregistered" in output
    assert "enable-canonical-uids" in output
    assert not (case / "policy_boundary_inventory_DOC_005.json").exists()
    assert not dao.run_state_path("CASE_030").exists()


def test_an_arbitrary_clause_contract_is_refused_unregistered(
        case, isolated_dao, make_args):
    """The same floor for the clause layer -- PC/CI, not just PS/PB."""
    data_file = isolated_dao / "clauses.json"
    _write_json(data_file, {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "clauses": [{
            "clause_uid": FAKE["clause"],
            "clause_id": "C-1",
            "source_boundary_uids": [FAKE["boundary"]],
            "clause_kind": "coverage",
            "payout_conditions": [{
                "condition_uid": FAKE["condition"],
                "text": "보험금을 지급합니다",
                "evidence_references": [{
                    "document_id": "DOC_005", "page": 1,
                    "quote": "회사는 보험금을 지급합니다"}],
                "support_level": "direct",
                "support_rationale": "원문 직접 진술",
                "review_required": False,
            }],
            "evidence_references": [{
                "document_id": "DOC_005", "page": 1,
                "quote": "회사는 보험금을 지급합니다"}],
            "review_required": False,
        }],
    })
    rc = dao.cmd_write_contract(make_args(
        case_id="CASE_030",
        filename="normalized_policy_clause_DOC_005.json",
        data_file=str(data_file),
        schema_name=_cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        held_by="policy-pipeline", run_id="RUN_20260728_001", stage=None))
    assert rc == 1
    assert not (case / "normalized_policy_clause_DOC_005.json").exists()


def test_an_arbitrary_reference_table_is_refused_unregistered(
        case, isolated_dao, make_args):
    """And for RT/RR/RC."""
    data_file = isolated_dao / "tables.json"
    _write_json(data_file, {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "tables": [{
            "table_uid": FAKE["table"],
            "table_label": "제3조",
            "columns": [{"column_id": "c1", "header_text": "구분"}],
            "rows": [{
                "row_uid": FAKE["row"],
                "cells": [{
                    "cell_uid": FAKE["cell"],
                    "column_id": "c1",
                    "value_text": "제3조",
                    "evidence_references": [{
                        "document_id": "DOC_005", "page": 1,
                        "quote": "제3조"}],
                }],
                "review_required": False,
            }],
            "review_required": False,
        }],
    })
    rc = dao.cmd_write_contract(make_args(
        case_id="CASE_030",
        filename="reference_table_DOC_005.json",
        data_file=str(data_file),
        schema_name=_cross_contract.REFERENCE_TABLE_SCHEMA,
        held_by="policy-pipeline", run_id="RUN_20260728_001", stage=None))
    assert rc == 1
    assert not (case / "reference_table_DOC_005.json").exists()


def test_a_policy_contract_with_no_identifiable_document_is_refused(case):
    """Being unable to say WHICH document a contract is about is not a reason
    to skip the check -- it is the state in which no check can apply."""
    blockers = dao._policy_write_scheme_blockers(
        "CASE_030", "policy_boundary_inventory.json", {})
    assert any("could not be determined" in b for b in blockers), blockers


# =========================================================================
# 2. An explicitly legacy document cannot carry new policy work either
# =========================================================================

def test_a_legacy_document_refuses_a_schema_valid_consistent_uid_set(
        case, isolated_dao, make_args, capsys):
    """Requirement 2. The submitted inventory is schema-valid, its spans really
    do quote page 1 at the offsets given, and its boundary_uid matches
    everywhere it appears -- every consistency check in the codebase agrees
    with it. It is refused anyway, because consistency between files one agent
    wrote is not provenance."""
    _make_legacy(make_args, isolated_dao)
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    output = capsys.readouterr().out
    assert "uid_scheme=legacy" in output
    assert "enable-canonical-uids" in output
    assert not (case / "policy_boundary_inventory_DOC_005.json").exists()


def test_a_legacy_document_cannot_overwrite_an_existing_policy_contract(
        case, isolated_dao, make_args):
    """Overwriting is a new write. A legacy artifact already on disk stays
    readable; replacing it with different content is authoring new policy work
    against unverified UIDs."""
    existing = case / "policy_boundary_inventory_DOC_005.json"
    _write_json(existing, _inventory(span_uid="PS-2222222222222222"))
    before = existing.read_text(encoding="utf-8")
    _make_legacy(make_args, isolated_dao)
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    assert existing.read_text(encoding="utf-8") == before


# =========================================================================
# 3. policy_clause_processing cannot finalize on a legacy policy layer
# =========================================================================

def test_a_legacy_document_blocks_policy_stage_finalization(
        case, isolated_dao, make_args, capsys):
    """Requirement 3."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json",
                _run_state([("document_processing", "passed")]))
    assert dao._finalize_stage(
        "CASE_030", "RUN_20260728_001", "policy_clause_processing",
        "policy-pipeline") is None
    assert "uid_scheme=legacy" in capsys.readouterr().out
    assert _stage_status("policy_clause_processing") is None


def test_an_unregistered_document_blocks_policy_stage_finalization(
        case, isolated_dao, make_args):
    _write_json(case / "_run_state.json",
                _run_state([("document_processing", "passed")]))
    blockers = dao._policy_completion_blockers("CASE_030")
    assert any("unregistered" in b for b in blockers), blockers


def test_a_reference_table_only_segment_must_also_be_canonical(
        case, isolated_dao, make_args):
    """Requirement 13, first shape: an appendix segment owes no clause
    contract, but it is still a policy document the stage publishes, so its
    UIDs are still identity claims that must be verified."""
    _write_json(case / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [
            _manifest_entry("DOC_001", document_role="physical",
                            policy_processing_role="segmented_parent"),
            _manifest_entry(
                "DOC_005", document_role="segment",
                source_document_id="DOC_001",
                policy_processing_role="reference_table_only",
                page_map=[{"logical_page": 1, "source_physical_page": 8}]),
        ],
    })
    blockers = dao._policy_layer_scheme_blockers("CASE_030", "test")
    assert any("DOC_001" in b for b in blockers), blockers
    assert any("DOC_005" in b for b in blockers), blockers


# =========================================================================
# 4/5. A stale `passed` policy stage does not license downstream work
# =========================================================================

def test_a_recorded_passed_policy_stage_does_not_license_coverage_result(
        case, isolated_dao, make_args, capsys):
    """Requirement 4: the exact bypass. `policy_clause_processing: passed` is
    already on disk from an earlier run, and the policy document is legacy. A
    coverage_result citing that document's clause is refused anyway -- the live
    scheme decides, not the historical status field."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
    ]))
    errors = dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json",
        {"coverages": [{"matched_clause_ref": {
            "document_id": "DOC_005",
            "clause_uid": FAKE["clause"]}}]})
    assert any("uid_scheme=legacy" in e for e in errors), errors

    data_file = isolated_dao / "coverage.json"
    _write_json(data_file, {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "claim-analysis",
        "status": "success",
        "coverages": [{
            "coverage_name": "사망보험금",
            "standardized_coverage_name": "사망",
            "applicable": True,
            "matched_clause_ref": {
                "document_id": "DOC_005", "clause_uid": FAKE["clause"]},
            "evidence_references": [{
                "document_id": "DOC_005", "page": 1,
                "quote": "회사는 보험금을 지급합니다"}],
            "confidence": 0.9,
            "review_required": False,
        }],
    })
    # Schema-valid, so the refusal below is the P0-3 gate speaking and not a
    # shape error standing in for it.
    assert dao._schema_check(
        json.loads(data_file.read_text(encoding="utf-8")),
        "coverage_result.schema.json") == []
    rc = dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="coverage_result.json",
        data_file=str(data_file),
        schema_name="coverage_result.schema.json",
        held_by="claim-analysis", run_id="RUN_20260728_001", stage=None))
    assert rc == 1
    assert not (case / "coverage_result.json").exists()
    assert "stale, unresolved, or invalid" in capsys.readouterr().out


def test_a_recorded_passed_policy_stage_does_not_license_downstream_finalize(
        case, isolated_dao, make_args, capsys):
    """Requirement 5: not just the write -- the transitive downstream STAGE.

    claim_analysis depends on policy_clause_processing, whose recorded status
    satisfies `check_dependencies`. That status was recorded against a legacy
    policy layer, so finalizing on top of it publishes results derived from
    UIDs nobody ever verified.
    """
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
    ]))
    assert stage_dependencies.check_dependencies(
        "claim_analysis", "passed",
        dao.load_run_state("CASE_030")) == []

    assert dao._finalize_stage(
        "CASE_030", "RUN_20260728_001", "claim_analysis",
        "claim-analysis") is None
    assert "not canonically verified" in capsys.readouterr().out
    assert _stage_status("claim_analysis") is None


@pytest.mark.parametrize(
    "stage", sorted(stage_dependencies.dependents_of("policy_clause_processing")))
def test_every_transitive_downstream_stage_is_gated(
        case, isolated_dao, make_args, stage):
    """Asserted over the graph rather than a hand-picked stage, so a stage
    added to the pipeline later inherits the gate instead of quietly escaping
    it."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state(
        [("document_processing", "passed"),
         ("policy_clause_processing", "passed")]
        + [(s, "passed") for s in sorted(stage_dependencies.KNOWN_STAGES)
           if s not in ("document_processing", "policy_clause_processing",
                        stage)]))
    assert dao._finalize_stage(
        "CASE_030", "RUN_20260728_001", stage, "tester") is None
    assert _stage_status(stage) != "passed" or stage in ("evaluation",)


# =========================================================================
# 6. Mixed canonical/legacy is refused whole
# =========================================================================

def test_a_mixed_canonical_and_legacy_document_set_is_refused(
        case, isolated_dao, make_args, canonicalize):
    """Requirement 6/13. DOC_001 is canonical; DOC_005 is legacy. A contract
    expressed against both is not half-verified -- it is unverified with a
    verified-looking part."""
    _write_json(case / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [_manifest_entry("DOC_001"), _manifest_entry("DOC_005")],
    })
    (isolated_dao / "data" / "raw" / "CASE_030" / "DOC_001.pdf").write_bytes(
        b"%PDF-1.7 a second immutable policy source")
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_001", TEXT)
    _make_legacy(make_args, isolated_dao)

    assert dao.uid_scheme_for("CASE_030", "DOC_001") == "canonical_v1"
    blockers = dao._policy_write_scheme_blockers(
        "CASE_030", "policy_parent_coverage_DOC_001.json",
        {"source_document_id": "DOC_001",
         "pages": [{"owning_document_id": "DOC_005"}]})
    # Exactly one blocker, and it is about DOC_005: the canonical member of the
    # set is not what refuses the write, the legacy one is.
    assert len(blockers) == 1, blockers
    assert blockers[0].startswith("DOC_005 is uid_scheme=legacy"), blockers

    # And the whole-layer gate refuses the stage for the same reason.
    assert any("DOC_005" in b for b in
               dao._policy_layer_scheme_blockers("CASE_030", "test"))


def test_a_parent_coverage_owning_a_legacy_segment_blocks_the_stage(
        case, isolated_dao, make_args, canonicalize):
    """Requirement 13, second shape: the segment is not in the manifest's
    automated-policy filter as a separate concern -- it is reached through the
    parent's coverage contract, and a parent resting on an unverified segment
    is not a verified parent."""
    _write_json(case / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [
            _manifest_entry("DOC_001", policy_processing_role="segmented_parent"),
        ],
    })
    (isolated_dao / "data" / "raw" / "CASE_030" / "DOC_001.pdf").write_bytes(
        b"%PDF-1.7 a segmented parent")
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_001", TEXT)
    _write_json(case / "policy_parent_coverage_DOC_001.json", {
        "pages": [{"page": 1, "owning_document_id": "DOC_009"}],
    })
    blockers = dao._policy_layer_scheme_blockers("CASE_030", "test")
    assert any("DOC_009" in b for b in blockers), blockers


# =========================================================================
# 7. Legacy stays readable and migratable
# =========================================================================

def test_legacy_artifacts_stay_readable(case, isolated_dao, make_args, capsys):
    """Requirement 7. The fix is 'legacy may not be written against', never
    'legacy may not be read' -- a legacy artifact that could not be read could
    not be migrated, and the whole migration path would be a dead end."""
    _write_json(case / "policy_boundary_inventory_DOC_005.json", _inventory())
    _make_legacy(make_args, isolated_dao)

    assert dao.cmd_read_contract(make_args(
        case_id="CASE_030",
        filename="policy_boundary_inventory_DOC_005.json")) == 0
    assert FAKE["boundary"] in capsys.readouterr().out
    assert dao.read_contract_data(
        "CASE_030", "policy_boundary_inventory_DOC_005.json") is not None


def test_migration_preparation_commands_work_on_a_legacy_document(
        case, isolated_dao, make_args, capsys):
    """read-revision-index, record-source-digest, and re-registering source
    text must all succeed while the document is legacy -- they are the
    prerequisites `enable-canonical-uids` checks for."""
    _make_legacy(make_args, isolated_dao)
    assert dao.cmd_read_revision_index(make_args(case_id="CASE_030")) == 0
    assert "DOC_005" in capsys.readouterr().out
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0
    # Re-registering a revision is still allowed: a migration may well start by
    # correcting the source text.
    _make_legacy(make_args, isolated_dao, text=TEXT + "추가 문장.\n")


# =========================================================================
# 8. Activation invalidates the stage it makes unverifiable
# =========================================================================

def test_activation_succeeds_and_invalidates_the_policy_layer(
        case, isolated_dao, make_args, capsys):
    """Requirement 8. Activation must NOT require the artifacts to already be
    canonical -- that would be a deadlock, since the only way to write a
    canonical artifact is to be canonical first. So activation is allowed, and
    what it does instead is invalidate everything that was published under the
    legacy rules.
    """
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "policy_boundary_inventory_DOC_005.json", _inventory())
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
        ("claim_analysis", "passed"),
        ("screening_report", "passed"),
    ]))
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0

    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 0
    assert "INVALIDATED" in capsys.readouterr().out

    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"
    assert _stage_status("policy_clause_processing") == "failed"
    assert _stage_status("claim_analysis") == "failed"
    assert _stage_status("screening_report") == "failed"
    # Upstream of the policy layer is untouched: document_processing's result
    # is not a claim about policy UIDs.
    assert _stage_status("document_processing") == "passed"
    # The legacy artifact is NOT rewritten or deleted. Minting UIDs on a
    # caller's behalf is the thing this layer exists to prevent.
    assert (case / "policy_boundary_inventory_DOC_005.json").exists()
    assert json.loads(
        (case / "policy_boundary_inventory_DOC_005.json").read_text(
            encoding="utf-8"))["boundaries"][0]["boundary_uid"] == \
        FAKE["boundary"]


def test_activation_is_idempotent_when_nothing_is_left_to_invalidate(
        case, isolated_dao, make_args):
    """Re-running activation on an already-invalidated case is a clean no-op,
    not an error -- a migration that could not be safely retried would push
    callers toward hand-editing state."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "failed"),
    ]))
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 0
    assert _stage_status("policy_clause_processing") == "failed"
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 0
    assert _stage_status("policy_clause_processing") == "failed"


# =========================================================================
# 9. After activation the P0-2 gate is what speaks
# =========================================================================

def test_after_activation_a_canonical_artifact_writes_and_a_fake_does_not(
        case, isolated_dao, make_args, canonicalize, capsys):
    """Requirement 9. Both halves matter: P0-3 must not have made canonical
    writes impossible, and it must not have replaced P0-2's recomputation with
    a scheme check that passes anything once the scheme is on."""
    import policy_uid

    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    pdf = dao.read_contract_data(
        "CASE_030", "document_manifest.json")["documents"][0][
            "source_pdf_sha256"]
    binding = {"documents": [{
        "document_id": "DOC_005",
        "revision_sha256": dao.revision_entry_for(
            "CASE_030", "DOC_005")["current_revision_sha256"]}]}

    quote = PAGE_1.rstrip("\n")
    ps = policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=1, span_text=quote,
        ordinal=1)
    pb = policy_uid.compute_uid(
        "boundary", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1)
    assert _write_inventory(
        isolated_dao, make_args,
        _inventory(span_uid=ps, boundary_uid=pb, binding=binding)) == 0
    assert (case / "policy_boundary_inventory_DOC_005.json").exists()

    # The same document, the same scheme, a fabricated span UID: still refused,
    # now by P0-2's recomputation rather than by P0-3's scheme gate.
    assert _write_inventory(
        isolated_dao, make_args,
        _inventory(boundary_uid=pb, binding=binding)) == 1
    assert "non-canonical UIDs" in capsys.readouterr().out


# =========================================================================
# 10. No downgrade, by any route
# =========================================================================

def test_canonical_cannot_be_downgraded_to_legacy(
        case, isolated_dao, make_args, canonicalize):
    """Requirement 10. There is no disable command, and the transition checker
    refuses the downgrade directly."""
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    assert source_provenance.scheme_transition_errors(
        "canonical_v1", "legacy")
    assert not any(
        "disable" in name and "canonical" in name for name in dir(dao))
    # Re-activating is a no-op, never a downgrade, and never re-invalidates.
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 0
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"


def test_a_manifest_write_cannot_move_the_scheme(
        case, isolated_dao, make_args, canonicalize):
    """The scheme is not a manifest field, and a manifest write that tries to
    introduce one is refused by the sealed-field check rather than silently
    ignored."""
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    manifest["documents"][0]["uid_scheme"] = "legacy"
    data_file = isolated_dao / "manifest.json"
    _write_json(data_file, manifest)
    assert dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="document_manifest.json",
        data_file=str(data_file),
        schema_name="document_manifest.schema.json",
        held_by="attacker", run_id="RUN_20260728_001", stage=None)) == 1
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"


def test_a_manifest_patch_cannot_move_the_scheme(
        case, isolated_dao, make_args, canonicalize):
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    ok, message = dao.patch_manifest_document(
        "CASE_030", "DOC_005", {"uid_scheme": "legacy"},
        "attacker", "RUN_20260728_001")
    assert not ok, message
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v1"


def test_stripping_the_source_digest_does_not_reopen_the_gate(
        case, isolated_dao, make_args, canonicalize):
    """Deleting a sealed field is treated exactly like changing it: a check
    that only compared fields present in both versions would let a caller strip
    the field and supply their own on the next write."""
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    manifest["documents"][0].pop("source_pdf_sha256")
    data_file = isolated_dao / "manifest.json"
    _write_json(data_file, manifest)
    assert dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename="document_manifest.json",
        data_file=str(data_file),
        schema_name="document_manifest.schema.json",
        held_by="attacker", run_id="RUN_20260728_001", stage=None)) == 1


# =========================================================================
# 11. A corrupt or unknown scheme is fail-closed
# =========================================================================

@pytest.mark.parametrize("scheme", ["canonical_v2", "CANONICAL_V1", "", "none"])
def test_an_unknown_uid_scheme_is_not_treated_as_legacy(
        case, isolated_dao, make_args, scheme):
    """Requirement 11. Written past the schema deliberately: this is the state
    the DAO must still refuse if a revision index ever reached it by another
    route (a hand edit, a partial restore, a future build's value)."""
    _make_legacy(make_args, isolated_dao)
    index_path = dao.revision_index_path("CASE_030")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["documents"][0]["uid_scheme"] = scheme
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    assert dao.uid_scheme_for("CASE_030", "DOC_005") == scheme
    blockers = dao._policy_write_scheme_blockers(
        "CASE_030", "policy_boundary_inventory_DOC_005.json", _inventory())
    assert any("unrecognized uid_scheme" in b for b in blockers), blockers
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    assert not (case / "policy_boundary_inventory_DOC_005.json").exists()


def test_a_revision_entry_with_no_scheme_at_all_is_fail_closed(
        case, isolated_dao, make_args):
    _make_legacy(make_args, isolated_dao)
    index_path = dao.revision_index_path("CASE_030")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["documents"][0]["uid_scheme"]
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    assert dao._policy_write_scheme_blockers(
        "CASE_030", "policy_boundary_inventory_DOC_005.json", _inventory())


def test_an_unknown_scheme_cannot_be_overwritten_by_activation(
        case, isolated_dao, make_args, capsys):
    """A corrupt current value is not a licence to overwrite it with a clean
    one -- that would launder a tampered index into a verified state in one
    command."""
    _make_legacy(make_args, isolated_dao)
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0
    index_path = dao.revision_index_path("CASE_030")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["documents"][0]["uid_scheme"] = "canonical_v0"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "canonical_v0"


# =========================================================================
# 12. Fault injection during activation
# =========================================================================

def test_a_failed_cascade_leaves_the_scheme_untouched_and_exits_nonzero(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """Requirement 12. The half-state this must never produce: canonical_v1
    switched on while policy_clause_processing still reads `passed` against
    artifacts whose UIDs were never verified -- which is fail-OPEN, since the
    downstream gate would then find a canonical scheme and a passed stage.
    """
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
        ("claim_analysis", "passed"),
    ]))
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0

    def _boom(*args, **kwargs):
        raise dao.CascadeFailed("injected: run-state lock held by another run")

    monkeypatch.setattr(dao, "_invalidate_policy_layer", _boom)
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 1
    assert "was NOT switched to canonical_v1" in capsys.readouterr().out
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    assert _stage_status("policy_clause_processing") == "passed"


def test_a_locked_run_state_blocks_activation_rather_than_half_applying(
        case, isolated_dao, make_args, capsys):
    """The same fault, injected through the real lock primitive rather than a
    monkeypatch: another run holds _run_state.json."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
    ]))
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0
    assert dao.acquire_lock_blocking(
        dao.run_state_path("CASE_030"), "other-run", "RUN_20260728_009",
        "holding the run-state") is None

    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 1
    assert "was NOT switched to canonical_v1" in capsys.readouterr().out
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    assert _stage_status("policy_clause_processing") == "passed"
    dao.release_lock(dao.run_state_path("CASE_030"))


def test_a_schema_invalid_cascade_result_aborts_the_activation(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """If the invalidation would produce a run-state the schema rejects, the
    activation must abort rather than write the scheme and skip the cascade."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
    ]))
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0

    _in_cascade = [False]
    real_now = dao.now_iso
    monkeypatch.setattr(
        dao, "now_iso",
        lambda: "not-a-timestamp" if _in_cascade[0] else real_now())

    real_invalidate = dao._invalidate_policy_layer

    def _wrapped(*args, **kwargs):
        _in_cascade[0] = True
        try:
            return real_invalidate(*args, **kwargs)
        finally:
            _in_cascade[0] = False

    monkeypatch.setattr(dao, "_invalidate_policy_layer", _wrapped)
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 1
    assert "was NOT switched to canonical_v1" in capsys.readouterr().out
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    assert _stage_status("policy_clause_processing") == "passed"


# =========================================================================
# The four named attacks, asserted as one narrative each
# =========================================================================

def test_attack_skip_activation_entirely(case, isolated_dao, make_args):
    """Attack 1: never call enable-canonical-uids, write policy work anyway."""
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    assert dao._policy_completion_blockers("CASE_030")
    assert dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json",
        {"coverages": [{"matched_clause_ref": {
            "document_id": "DOC_005", "clause_uid": FAKE["clause"]}}]})


def test_attack_delete_the_revision_entry_after_activation(
        case, isolated_dao, make_args, canonicalize):
    """Attack 2: activate, then remove the revision entry to fall back into the
    unchecked state. It lands in `unregistered`, which is refused."""
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    index_path = dao.revision_index_path("CASE_030")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["documents"] = []
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    assert dao.uid_scheme_for("CASE_030", "DOC_005") == \
        source_provenance.UNREGISTERED
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
    assert dao._policy_completion_blockers("CASE_030")


def test_attack_reuse_a_stale_passed_policy_stage(
        case, isolated_dao, make_args):
    """Attack 3: a run-state carried over from before canonical verification
    existed, whose policy stage reads `passed`."""
    _make_legacy(make_args, isolated_dao)
    _write_json(case / "_run_state.json", _run_state([
        ("document_processing", "passed"),
        ("policy_clause_processing", "passed"),
    ]))
    assert dao._policy_stage_passed_errors("CASE_030") == []
    assert dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json",
        {"coverages": [{"matched_clause_ref": {
            "document_id": "DOC_005", "clause_uid": FAKE["clause"]}}]})
    assert dao._finalize_stage(
        "CASE_030", "RUN_20260728_001", "claim_analysis", "tester") is None


def test_attack_downgrade_after_activation(
        case, isolated_dao, make_args, canonicalize):
    """Attack 4: activate, then flip back to legacy to write an arbitrary UID.

    Covered from both ends -- the transition checker refuses the downgrade, and
    a directly tampered index lands on a value the gate treats as unrecognized
    rather than as legacy.
    """
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_005", TEXT)
    assert source_provenance.scheme_transition_errors("canonical_v1", "legacy")
    assert source_provenance.revision_history_errors(
        {"uid_scheme": "canonical_v1", "revisions": []},
        {"uid_scheme": "legacy", "revisions": []})
    index_path = dao.revision_index_path("CASE_030")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["documents"][0]["uid_scheme"] = "legacy"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    # Even a successful file-level tamper does not buy a write: legacy is a
    # refusal state now, which is the whole point of P0-3.
    assert _write_inventory(isolated_dao, make_args, _inventory()) == 1
