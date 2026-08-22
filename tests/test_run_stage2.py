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
            _doc("DOC_004", file_format="pdf", segmentation_status="pending_review"),
            _doc("DOC_005", file_format="pdf", segmentation_status="required"),
            _doc("DOC_006", segmentation_status="completed"),
        ]}
        assert [d["document_id"] for d in s2.pending_bundles(manifest)] == [
            "DOC_004", "DOC_005"]

    def test_non_pdf_pending_review_is_not_a_segmentation_target(self) -> None:
        manifest = {"documents": [
            _doc("DOC_001", file_format="text", segmentation_status="pending_review"),
        ]}
        assert s2.pending_bundles(manifest) == []

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

    # ------------------------------------------------ reclassification --

    def _mixed(self):
        """A manifest with both split children and a top-level document.

        Every real document carries `file_path`; the fixtures above omit it
        because the default selection never reads it.
        """
        return {"documents": [
            _doc("DOC_006", source_file_name="b.pdf", document_type=None,
                 file_path="data/raw/CASE_X/b.pdf"),
            _doc("DOC_007", source_file_name="b.pdf", document_type="other",
                 file_path="data/raw/CASE_X/b.pdf"),
            _doc("DOC_008", source_file_name="b.pdf",
                 document_type="diagnosis_certificate",
                 file_path="data/raw/CASE_X/b.pdf"),
            _doc("DOC_005", source_file_name="orig.pdf", document_type=None,
                 downstream_disposition="superseded_bundle",
                 file_path="data/raw/CASE_X/orig.pdf"),
            # Never split -- one PDF, one document. No source_file_name.
            _doc("DOC_001", document_type="other",
                 file_path="data/raw/CASE_X/DOC_001.pdf"),
        ]}

    def test_reclassify_reaches_a_top_level_document(self) -> None:
        """The gap the first corpus pass fell into.

        `source_file_name` is the right filter for "who still owes a verdict",
        because a top-level document is classified inside checkpoint 1. It is
        the wrong filter for "whose verdict predates the taxonomy": that is
        answered identically whether a PDF held one document or thirty.

        Carrying the child-only filter into reclassification silently skipped
        13 of the corpus's 184 `other` documents -- ten of them 법률질의회신서
        read at 0.95 confidence, whose `legal_opinion` bucket had existed since
        2026-08-20. Nothing failed; they were simply never attempted.
        """
        picked = [d["document_id"] for d in
                  s2.unclassified_children(self._mixed(), "other")]

        assert "DOC_001" in picked

    def test_default_selection_still_ignores_a_top_level_document(self) -> None:
        """Widening is scoped to reclassification.

        In the default pass a top-level document has already been classified by
        checkpoint 1, so selecting it would re-run work that just happened.
        """
        picked = [d["document_id"] for d in
                  s2.unclassified_children(self._mixed())]

        assert "DOC_001" not in picked

    def test_expert_review_only_is_never_reclassified(self) -> None:
        """Excluded from the text pipeline, so a new type changes nothing."""
        manifest = {"documents": [
            _doc("DOC_010", document_type="other",
                 file_path="data/raw/CASE_X/DOC_010.pdf",
                 downstream_disposition="expert_review_only"),
        ]}

        for mode in (None, "other", "all"):
            assert s2.unclassified_children(manifest, mode) == [], mode

    def test_reclassify_other_reopens_only_the_catch_all_bucket(self) -> None:
        """The taxonomy-change case.

        A document's type is a verdict under one taxonomy, and the taxonomy
        moves: three administrative codes were added 2026-08-22, and every case
        classified earlier keeps its old answer because `not document_type` is
        false for it. Measured on a CASE_701 fork the next day -- Stage 2
        completed, made zero classification calls, and the manifest came out
        byte-identical.

        `other` is the safe width: a document already in the catch-all has no
        verdict to lose.
        """
        assert [d["document_id"] for d in
                s2.unclassified_children(self._mixed(), "other")] == [
                    "DOC_006", "DOC_007", "DOC_001"]

    def test_reclassify_all_reopens_settled_verdicts_too(self) -> None:
        """For a prompt or model change, where the old answers are in doubt."""
        assert [d["document_id"] for d in
                s2.unclassified_children(self._mixed(), "all")] == [
                    "DOC_006", "DOC_007", "DOC_008", "DOC_001"]

    def test_reclassify_never_touches_a_superseded_bundle(self) -> None:
        """`all` must not be read as "everything".

        A bundle's `document_type` is null by construction, not by omission,
        and `run` on one would re-OCR pages its children already own.
        """
        for mode in (None, "other", "all"):
            picked = [d["document_id"] for d in
                      s2.unclassified_children(self._mixed(), mode)]
            assert "DOC_005" not in picked, mode

    def test_default_selection_is_unchanged(self) -> None:
        """Omitting the flag must behave exactly as before it existed."""
        assert [d["document_id"] for d in
                s2.unclassified_children(self._mixed())] == ["DOC_006"]

    def test_an_unknown_mode_is_refused_rather_than_ignored(self) -> None:
        """A typo must not silently select the default set."""
        with pytest.raises(ValueError, match="reclassify"):
            s2.unclassified_children(self._mixed(), "everything")

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


def test_stage2_scratch_is_never_under_outputs() -> None:
    """Transient payloads are not shared contracts and must not bypass DAO."""
    assert s2.SCRATCH_ROOT == s2.ROOT / "_stage2_scratch"
    assert s2.ROOT / "outputs" not in s2.SCRATCH_ROOT.parents


def test_top_level_provider_reaches_segmentation_propose() -> None:
    src = (TOOLS / "run_stage2.py").read_text(encoding="utf-8")
    assert 'propose_argv += ["--provider", provider]' in src


def test_segmentation_precedes_any_classification() -> None:
    """Bundles must be P8-cleared/redacted and split before type inference."""
    src = (TOOLS / "run_stage2.py").read_text(encoding="utf-8")
    assert src.index("# ---- phase 3: segmentation") < src.index(
        "# ---- phase 4: classify split children")


# ------------------------------------------- child classification concurrency --

def test_child_classification_runs_children_concurrently(monkeypatch) -> None:
    """The CASE_701 finding: 10 split children classified one subprocess at a time.

    Measured on that run the provider calls summed to 60.4s but occupied 81.4s of
    wall, the 21.1s difference being interpreter starts between calls (mean 2.34s
    of dead air). No child depends on another's verdict: each writes its own
    `classification_result_DOC_X.json` and patches the manifest through
    `dao.patch_manifest_document`, a read-modify-write under one lock hold.

    A barrier is the assertion. A serial loop cannot clear it -- child 1 waits for
    child 2, which is never started until child 1 returns -- so this fails by
    timeout rather than by comparing durations, which would only measure the
    machine's mood.
    """
    import threading

    children = [f"DOC_{n:03d}" for n in range(10, 14)]
    barrier = threading.Barrier(len(children), timeout=10)
    lock = threading.Lock()
    seen: list[str] = []
    broke = []

    def fake_run(argv, *, phase, progress):
        if phase.startswith("classify:"):
            with lock:
                seen.append(phase)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                broke.append(phase)
        return {"phase": phase, "returncode": 0,
                "result": {"status": "passed"}, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s2, "_run", fake_run)
    results = s2._classify_children(
        "CASE_701", [_doc(d) for d in children],
        common=lambda extra: extra, provider="claude-cli",
        workers=len(children), report=lambda msg: None)

    assert not broke, (
        "child classification dispatched serially: independent per-document "
        "classifications never overlapped")
    assert sorted(seen) == sorted(f"classify:{d}" for d in children)
    assert len(results) == len(children)


def test_child_classification_reports_the_first_failure(monkeypatch) -> None:
    """A failing child must surface as a failed step, not vanish into a future.

    Concurrency must not convert a halt into a silent pass -- a stage marked
    `passed` whose classification never ran withholds a downstream input.
    """
    def fake_run(argv, *, phase, progress):
        ok = not phase.endswith("DOC_012")
        return {"phase": phase, "returncode": 0 if ok else 1,
                "result": {"status": "passed" if ok else "failed"},
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(s2, "_run", fake_run)
    results = s2._classify_children(
        "CASE_701", [_doc(f"DOC_{n:03d}") for n in (10, 11, 12, 13)],
        common=lambda extra: extra, provider=None, workers=4,
        report=lambda msg: None)

    failed = [r for r in results if not s2._phase_ok(r)]
    assert [r["phase"] for r in failed] == ["classify:DOC_012"]


def test_child_classification_preserves_manifest_order(monkeypatch) -> None:
    """Steps are recorded in manifest order regardless of completion order.

    The receipt is read by a human comparing it against the manifest; ordering
    it by whichever provider call happened to return first would make two runs
    of the same case produce different-looking receipts.
    """
    import random

    def fake_run(argv, *, phase, progress):
        # Completion order deliberately unrelated to submission order.
        if phase.endswith("DOC_011"):
            import time
            time.sleep(0.05)
        return {"phase": phase, "returncode": 0,
                "result": {"status": "passed"}, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s2, "_run", fake_run)
    order = [f"DOC_{n:03d}" for n in (10, 11, 12, 13)]
    results = s2._classify_children(
        "CASE_701", [_doc(d) for d in order],
        common=lambda extra: extra, provider=None, workers=4,
        report=lambda msg: None)

    assert [r["phase"] for r in results] == [f"classify:{d}" for d in order]


def test_classify_default_and_call_site_are_both_concurrent() -> None:
    """Pins the production wiring, not just the helper.

    The concurrency tests above pass `workers` explicitly, so they would keep
    passing if the DEFAULT dropped to 1 or the driver's call site hard-coded
    `workers=1` -- the helper would still be parallel and the real run serial.
    Verified by reintroducing exactly that: with the call site changed the
    barrier test still passed, which is how this gap was found.
    """
    assert s2._CLASSIFY_WORKERS > 1
    src = (TOOLS / "run_stage2.py").read_text(encoding="utf-8")
    assert "workers=doc_workers, report=report)" in src, (
        "the driver must pass the operator's --doc-workers through; a literal "
        "here would silently serialize the real run")
    # `doc_workers` is None unless the operator passes --doc-workers, so the
    # default must be what actually applies on a plain invocation.
    assert s2._classify_children.__defaults__ is None


def test_classify_none_workers_falls_back_to_the_concurrent_default(monkeypatch) -> None:
    """A plain run passes doc_workers=None; that must not mean "one at a time"."""
    import threading

    children = [_doc(f"DOC_{n:03d}") for n in range(10, 14)]
    barrier = threading.Barrier(len(children), timeout=10)
    broke = []

    def fake_run(argv, *, phase, progress):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            broke.append(phase)
        return {"phase": phase, "returncode": 0,
                "result": {"status": "passed"}, "stdout": "", "stderr": ""}

    monkeypatch.setattr(s2, "_run", fake_run)
    s2._classify_children("CASE_701", children, common=lambda e: e,
                          provider=None, workers=None, report=lambda m: None)
    assert not broke, "workers=None must use the concurrent default, not 1"
