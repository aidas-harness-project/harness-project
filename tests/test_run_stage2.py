"""The Stage 2 driver: phase selection, gate stops, and the status contract.

`run_stage2.py` exists because Stage 2's step sequence was serialized in a SPEC
rather than in code -- the agent invoked six tools one at a time and paid a
model round trip between each. Measured on CASE_911 that was ~510s of 897s.

These cover the parts that decide CORRECTNESS rather than speed:

  * the phase-status contract -- an earlier version omitted `passed`, the value
    `classify-only` returns on success, so the driver halted on a correctly
    classified document while the tool itself exited 0;
  * the gates -- the driver must STOP at a human decision, never answer it;
  * the contract envelope `chunk_text.py` does not emit, which used to be an
    undocumented hand-filled step.

Every test here is offline: no subprocess, no provider, no real case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_stage2 as s2  # noqa: E402


def _doc(doc_id: str, **kw) -> dict:
    base = {"document_id": doc_id}
    base.update(kw)
    return base


# ------------------------------------------------------------ status contract --

class TestPhaseStatusContract:
    """What counts as "this phase did its job"."""

    @pytest.mark.parametrize("status", sorted(s2._PHASE_OK_STATUSES))
    def test_success_statuses_pass(self, status: str) -> None:
        assert s2._phase_ok({"returncode": 0, "result": {"status": status}})

    def test_passed_is_accepted(self) -> None:
        """The regression that actually happened.

        `run_checkpoint1.py classify-only` returns `passed`. An earlier
        allow-list had only {success, already_split, already_extracted,
        no_work}, so the driver stopped on a document that had just been
        classified correctly -- the tool exited 0 and did its work, and only
        the driver disagreed.
        """
        assert "passed" in s2._PHASE_OK_STATUSES
        assert s2._phase_ok({"returncode": 0, "result": {"status": "passed"}})

    @pytest.mark.parametrize("status", [
        "blocked_disagreement",    # P8 -- a human picks the reading
        "blocked_segmentation",
        "partially_resolved",
        "not_ready",
        "partial",
        "inconsistent_existing_split",
        "manifest_write_failed",
        "failed",
        "error",
    ])
    def test_blocking_statuses_stop_the_driver(self, status: str) -> None:
        assert not s2._phase_ok({"returncode": 0, "result": {"status": status}})

    def test_unknown_status_stops_rather_than_passes(self) -> None:
        """Fail closed: a status nobody has classified must surface as a stop.

        The allow-list is spelled out instead of derived precisely so that a
        new status added upstream shows up here as something to look at.
        """
        assert not s2._phase_ok(
            {"returncode": 0, "result": {"status": "some_new_status"}})

    def test_nonzero_exit_always_fails(self) -> None:
        assert not s2._phase_ok({"returncode": 1, "result": {"status": "success"}})

    def test_clean_exit_without_json_is_ok(self) -> None:
        """`dao.py write-contract` prints no JSON verdict."""
        assert s2._phase_ok({"returncode": 0, "result": None})


# ---------------------------------------------------------------- selectors --

class TestSelectors:
    def test_pending_bundle_is_one_marked_required(self) -> None:
        manifest = {"documents": [
            _doc("DOC_001", segmentation_status="not_required"),
            _doc("DOC_005", segmentation_status="required"),
            _doc("DOC_006", segmentation_status="completed"),
        ]}
        assert [d["document_id"] for d in s2.pending_bundles(manifest)] == ["DOC_005"]

    def test_unclassified_children_are_split_children_without_a_type(self) -> None:
        manifest = {"documents": [
            # a top-level document is not a child -- no source_file_name
            _doc("DOC_001", document_type=None),
            _doc("DOC_006", source_file_name="DOC_005.pdf", document_type=None),
            _doc("DOC_007", source_file_name="DOC_005.pdf",
                 document_type="diagnosis_certificate"),
        ]}
        assert [d["document_id"] for d in
                s2.unclassified_children(manifest)] == ["DOC_006"]

    def test_superseded_bundle_is_never_a_classification_target(self) -> None:
        """Its children own its pages; it owes no document_type."""
        manifest = {"documents": [
            _doc("DOC_005", source_file_name="orig.pdf", document_type=None,
                 downstream_disposition="superseded_bundle"),
        ]}
        assert s2.unclassified_children(manifest) == []

    def test_chunkable_splits_text_from_excluded(self) -> None:
        manifest = {"documents": [
            _doc("DOC_001", redacted_text_path="p/x.md"),
            _doc("DOC_002", downstream_disposition="expert_review_only"),
            _doc("DOC_005", downstream_disposition="superseded_bundle",
                 redacted_text_path="p/bundle.md"),
            _doc("DOC_006", redacted_text_path=None),
        ]}
        text, excluded = s2.chunkable_documents(manifest)
        assert text == ["DOC_001"], "only redacted, non-bundle documents"
        assert excluded == ["DOC_002"]

    def test_bundle_excluded_from_chunking_even_with_redacted_text(self) -> None:
        """The bundle keeps a redacted_text_path until it is superseded, and
        every one of its pages is also a child's page -- chunking it would
        count the same page twice."""
        manifest = {"documents": [
            _doc("DOC_005", downstream_disposition="superseded_bundle",
                 redacted_text_path="p/bundle.md"),
        ]}
        assert s2.chunkable_documents(manifest) == ([], [])


# -------------------------------------------------------------------- gates --

class TestGates:
    def test_segmentation_approval_is_not_auto_by_default(self) -> None:
        """The boundary gate must require an explicit opt-out.

        A reviewer normally sees the proposed ranges and the title line each
        cut is made on; `--auto-approve-segmentation` is a timing/plumbing
        flag, orchestrator-owned like `--single-reader`.
        """
        import inspect
        sig = inspect.signature(s2.run_stage2)
        assert sig.parameters["auto_approve_segmentation"].default is False

    def test_driver_never_finalizes_a_stage(self) -> None:
        """T13: the orchestrator owns every run-state marker.

        A driver that finalized its own stage would make a passing marker mean
        "the tool ran" instead of "the orchestrator accepted the result".
        """
        src = (TOOLS / "run_stage2.py").read_text(encoding="utf-8")
        # Match an actual dao.py subcommand invocation, not the words appearing
        # in prose -- the module docstring and the success note both mention
        # finalize-stage precisely to say the driver does NOT call it.
        for subcommand in ('"finalize-stage"', '"update-run-state"',
                           "'finalize-stage'", "'update-run-state'"):
            assert subcommand not in src, (
                f"run_stage2.py invokes {subcommand}; run-state markers belong "
                "to the orchestrator (T13)"
            )


# ----------------------------------------------------------------- envelope --

def test_page_chunks_envelope_fields_are_added() -> None:
    """chunk_text.py emits the payload only; the schema needs the envelope.

    Writing the chunker's stdout straight through `write-contract` fails
    validation on case_id/component/status -- which is why this was a
    hand-filled step before the driver owned it.
    """
    src = (TOOLS / "run_stage2.py").read_text(encoding="utf-8")
    for field in ('"case_id": case_id', '"component": "document-pipeline"',
                  '"status": "success"', '"run_id": run_id', '"created_at"'):
        assert field in src, f"driver must set {field} on the page_chunks contract"
