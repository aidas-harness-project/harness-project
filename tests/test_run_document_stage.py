"""T8: checkpoint 1 across a case's documents, with document-level parallelism.

The properties here are the ones concurrency can silently take back. Two are
worth stating outright:

* Results must be keyed to their own document. A pool that appends as it
  completes files document B's OCR under document A -- the same class of bug
  the page-level tests guard against, one level up.
* A blocked document must NOT take its siblings down, and must still fail the
  step. This is deliberately the opposite of redaction's leak-halt (T4c): a
  possible PII leak is a privacy event, while a P8 disagreement is a
  per-document extraction failure. Both tests exist because "keep going" is
  easy to over-apply into "and report success".
"""
import threading
import time

import pytest

import run_document_stage as rds


def _manifest(*docs):
    return {"case_id": "CASE_009", "documents": list(docs)}


def _doc(doc_id, **extra):
    d = {"document_id": doc_id, "file_name": f"{doc_id}.pdf",
         "file_path": f"data/raw/CASE_009/{doc_id}.pdf", "ocr_status": "pending"}
    d.update(extra)
    return d


@pytest.fixture
def five_docs(monkeypatch):
    manifest = _manifest(*[_doc(f"DOC_00{i}") for i in range(1, 6)])
    monkeypatch.setattr(rds._dao, "read_contract_data",
                        lambda case_id, name: manifest)
    return manifest


def _install(monkeypatch, fn):
    monkeypatch.setattr(rds, "run_checkpoint1", fn)


# ------------------------------------------------------------- correctness --

def test_each_result_is_filed_under_its_own_document(five_docs, monkeypatch):
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        # Later documents finish first, so an append-as-completed
        # implementation would misfile every one of them.
        time.sleep(0.02 * (6 - int(doc_id[-1])))
        return {"status": "success", "doc_id": doc_id, "marker": doc_id}
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=4)

    assert out["status"] == "success"
    assert [d["doc_id"] for d in out["documents"]] == [f"DOC_00{i}" for i in range(1, 6)]
    for d in out["documents"]:
        assert d["marker"] == d["doc_id"], "a result was filed under the wrong document"


def test_every_document_is_processed_exactly_once(five_docs, monkeypatch):
    seen = []
    lock = threading.Lock()

    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        with lock:
            seen.append(doc_id)
        return {"status": "success"}
    _install(monkeypatch, fake)

    rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=3)
    assert sorted(seen) == [f"DOC_00{i}" for i in range(1, 6)]


def test_observed_concurrency_reaches_the_worker_count(five_docs, monkeypatch):
    barrier = threading.Barrier(3, timeout=10)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with lock:
            state["active"] -= 1
        return {"status": "success"}
    _install(monkeypatch, fake)

    rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=3)
    assert state["peak"] == 3


def test_sequential_and_parallel_produce_the_same_results(five_docs, monkeypatch):
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        return {"status": "success", "value": doc_id * 2}
    _install(monkeypatch, fake)

    seq = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=1)
    par = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=4)
    assert seq["documents"] == par["documents"]


# -------------------------------------------------------- failure isolation --

def test_a_blocked_document_does_not_stop_the_others(five_docs, monkeypatch):
    """The semantics chosen for T8: a P8 disagreement blocks its own document
    only. Letting DOC_004 finish does not make DOC_002's disagreement less
    blocking -- it just avoids throwing away work that succeeded."""
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        if doc_id == "DOC_002":
            return {"status": "blocked_disagreement", "doc_id": doc_id,
                    "disagreed_pages": [3]}
        return {"status": "success"}
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=3)

    done = {d["doc_id"]: d["status"] for d in out["documents"]}
    assert len(done) == 5, "a blocked document discarded its siblings' results"
    assert done["DOC_002"] == "blocked_disagreement"
    assert all(s == "success" for k, s in done.items() if k != "DOC_002")


def test_a_blocked_document_still_fails_the_step(five_docs, monkeypatch):
    """Continuing must never be reported as success -- P8 has no tolerance
    threshold, so one blocked page still fails the stage."""
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        return ({"status": "blocked_disagreement"} if doc_id == "DOC_002"
                else {"status": "success"})
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=3)

    assert out["status"] == "partial"
    assert [b["doc_id"] for b in out["blocked"]] == ["DOC_002"]


def test_a_raising_document_is_contained(five_docs, monkeypatch):
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        if doc_id == "DOC_003":
            raise RuntimeError("provider exploded")
        return {"status": "success"}
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=3)

    done = {d["doc_id"]: d for d in out["documents"]}
    assert len(done) == 5
    assert done["DOC_003"]["status"] == "error"
    assert "provider exploded" in done["DOC_003"]["error"]
    assert out["status"] == "partial"


def test_an_unknown_status_counts_as_blocked_not_passed(five_docs, monkeypatch):
    """_OK_STATUSES is an allowlist on purpose: a status added upstream must
    surface as a failure to look at, never be silently counted as a pass."""
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        return {"status": "some_new_status_nobody_told_us_about"}
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=2)
    assert out["status"] == "partial"
    assert len(out["blocked"]) == 5


# ------------------------------------------------------------ doc selection --

def test_superseded_bundle_and_completed_documents_are_skipped(monkeypatch):
    """The bundle is identified by `downstream_disposition`.

    This test previously built the bundle with
    `segmentation_status="superseded_bundle"` and passed -- but that is not a
    permitted value of that field (the enum is pending_review / required /
    not_required / completed / not_applicable), and the production filter read
    the same wrong field. Test and code shared one misunderstanding, so the
    filter was dead code and nothing noticed: on CASE_961 the bundle reached
    run_checkpoint1, was correctly refused with `blocked_segmentation`, and
    that correct refusal was reported as a blocking stage failure.
    """
    manifest = _manifest(
        _doc("DOC_001"),
        _doc("DOC_002", downstream_disposition="superseded_bundle"),
        _doc("DOC_003", ocr_status="completed"),
    )
    assert [d["document_id"] for d in rds.select_documents(manifest)] == ["DOC_001"]


def test_bundle_is_not_identified_by_segmentation_status(monkeypatch):
    """A guard against the exact confusion above coming back.

    `segmentation_status: completed` is what a real split bundle carries, and
    it must NOT by itself cause a skip -- every split CHILD is completed too.
    """
    manifest = _manifest(
        _doc("DOC_001", segmentation_status="completed"),
    )
    assert [d["document_id"] for d in rds.select_documents(manifest)] == ["DOC_001"], (
        "segmentation_status must not be used to identify the superseded bundle"
    )


def test_only_restricts_the_document_set(five_docs, monkeypatch):
    seen = []

    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        seen.append(doc_id)
        return {"status": "success"}
    _install(monkeypatch, fake)

    rds.run_document_stage("CASE_009", "tester", "RUN_1",
                           doc_workers=2, only=["DOC_002", "DOC_004"])
    assert sorted(seen) == ["DOC_002", "DOC_004"]


def test_a_case_with_nothing_to_do_succeeds(monkeypatch):
    monkeypatch.setattr(rds._dao, "read_contract_data",
                        lambda c, n: _manifest(_doc("DOC_001", ocr_status="completed")))
    out = rds.run_document_stage("CASE_009", "tester", "RUN_1")
    assert out["status"] == "success"
    assert out["documents"] == []


def test_a_missing_manifest_fails_rather_than_reporting_success(monkeypatch):
    monkeypatch.setattr(rds._dao, "read_contract_data", lambda c, n: None)
    out = rds.run_document_stage("CASE_009", "tester", "RUN_1")
    assert out["status"] == "failed"


# ---------------------------------------------------------------- worker cfg --

@pytest.mark.parametrize("raw,expected", [
    ("", rds.DEFAULT_DOC_WORKERS), ("2", 2), ("0", 1), ("-5", 1),
    ("abc", rds.DEFAULT_DOC_WORKERS),
])
def test_worker_resolution(monkeypatch, raw, expected):
    monkeypatch.setenv(rds.DOC_WORKERS_ENV, raw)
    assert rds._resolve_doc_workers(None) == expected


def test_explicit_worker_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv(rds.DOC_WORKERS_ENV, "8")
    assert rds._resolve_doc_workers(2) == 2


def test_default_is_three_measured(monkeypatch):
    """3 rather than the plan's proposed 2: measured on CASE_953, 3 workers
    reached 2.50x against a sequential baseline versus 1.58x at 2, with zero
    rate-limit errors, and the total in-flight risk the plan cited as the
    reason for 2 is now bounded by HARNESS_LLM_MAX_INFLIGHT instead."""
    monkeypatch.delenv(rds.DOC_WORKERS_ENV, raising=False)
    assert rds.DEFAULT_DOC_WORKERS == 3
    assert rds._resolve_doc_workers(None) == 3


def test_already_extracted_is_not_a_failure(five_docs, monkeypatch):
    """Found by running the driver for real, not by reading it.

    run_checkpoint1 returns `already_extracted` when a document already has
    OCR output -- that is its guard against paying for the same extraction
    twice, not an extraction failure. Counting it as blocked made a healthy
    re-run of a finished case exit non-zero.
    """
    def fake(case_id, doc_id, pdf, held_by, run_id, **kw):
        return {"status": "already_extracted", "doc_id": doc_id}
    _install(monkeypatch, fake)

    out = rds.run_document_stage("CASE_009", "tester", "RUN_1", doc_workers=2)

    assert out["status"] == "success"
    assert out["blocked"] == []
