"""Checkpoint 2 across a case's documents, with document-level parallelism.

Redaction was the last per-document sequential loop left in Stage 2: checkpoint
1 gained a case-wide driver (T8) while redaction stayed one
`redact_document.py` invocation per document, which on CASE_911 meant the two
scanned documents redacted one after the other even though nothing connects
them.

The properties are the same ones concurrency takes back silently, plus two that
belong to redaction specifically:

* Selection must not report a legitimate SKIP as a failure. A superseded
  bundle, an `expert_review_only` image, and an already-redacted document are
  all "nothing to do" -- and a driver that treats them as targets would either
  redact a bundle twice or feed a raw image to the text redactor.
* The redactor must be built through `redact_document._redactor_for`, not
  constructed here. That function is what routes a PII-free document class to
  the deterministic pass-through instead of paying for a model call; a second
  copy of that rule in this driver is a rule that will drift.
"""
import threading
import time

import pytest

import run_document_stage as rds


def _manifest(*docs):
    return {"case_id": "CASE_911", "documents": list(docs)}


def _doc(doc_id, **extra):
    d = {"document_id": doc_id, "file_name": f"{doc_id}.pdf",
         "ocr_status": "completed", "redacted_text_path": None}
    d.update(extra)
    return d


@pytest.fixture
def four_docs(monkeypatch):
    manifest = _manifest(*[_doc(f"DOC_00{i}") for i in range(1, 5)])
    monkeypatch.setattr(rds._dao, "read_contract_data",
                        lambda case_id, name: manifest)
    return manifest


@pytest.fixture(autouse=True)
def _no_real_redactor(monkeypatch):
    """Never construct a real provider in a unit test."""
    monkeypatch.setattr(rds.redact_document_mod, "_redactor_for",
                        lambda *a, **kw: object())


def _install(monkeypatch, fn):
    monkeypatch.setattr(rds.redact_document_mod, "redact_document", fn)


# ---------------------------------------------------------------- selection --

def test_superseded_bundle_is_never_redacted(monkeypatch):
    """The bundle is identified by `downstream_disposition`.

    This asserted `segmentation_status="superseded_bundle"` while that field's
    enum is pending_review/required/not_required/completed/not_applicable --
    the value can never appear there. The production filter read the same wrong
    field, so test and code shared one misunderstanding and the filter was dead
    code (found on CASE_961, where the identical mistake in select_documents
    made checkpoint 1 report the bundle's correct refusal as a stage failure).
    """
    manifest = _manifest(
        _doc("DOC_005", downstream_disposition="superseded_bundle"),
        _doc("DOC_006"))
    assert [d["document_id"] for d in rds.select_redaction_documents(manifest)] \
        == ["DOC_006"]


def test_expert_review_only_is_skipped(monkeypatch):
    """A non-text image has no page text; the redactor must never see it."""
    manifest = _manifest(
        _doc("DOC_001", downstream_disposition="expert_review_only"),
        _doc("DOC_002"))
    assert [d["document_id"] for d in rds.select_redaction_documents(manifest)] \
        == ["DOC_002"]


def test_an_already_redacted_document_is_not_redone(monkeypatch):
    manifest = _manifest(
        _doc("DOC_001", redacted_text_path="outputs/CASE_911/DOC_001/redacted_text.md"),
        _doc("DOC_002"))
    assert [d["document_id"] for d in rds.select_redaction_documents(manifest)] \
        == ["DOC_002"]


def test_a_document_without_completed_ocr_is_skipped(monkeypatch):
    """No page text exists yet -- this is a not-yet, not a failure."""
    manifest = _manifest(_doc("DOC_001", ocr_status="failed"), _doc("DOC_002"))
    assert [d["document_id"] for d in rds.select_redaction_documents(manifest)] \
        == ["DOC_002"]


def test_only_restricts_the_document_set(four_docs):
    picked = rds.select_redaction_documents(four_docs, only=["DOC_002"])
    assert [d["document_id"] for d in picked] == ["DOC_002"]


# -------------------------------------------------------------- correctness --

def test_each_result_is_filed_under_its_own_document(four_docs, monkeypatch):
    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        # Later documents finish first: an append-as-completed implementation
        # files every one of them under the wrong id.
        time.sleep(0.02 * (5 - int(doc_id[-1])))
        return {"status": "success", "pages": 3, "marker": doc_id}
    _install(monkeypatch, fake)

    out = rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=4)

    assert out["status"] == "success"
    assert [d["doc_id"] for d in out["documents"]] == [f"DOC_00{i}" for i in range(1, 5)]
    for d in out["documents"]:
        assert d["marker"] == d["doc_id"]


def test_every_document_is_redacted_exactly_once(four_docs, monkeypatch):
    seen = []
    lock = threading.Lock()

    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        with lock:
            seen.append(doc_id)
        return {"status": "success"}
    _install(monkeypatch, fake)

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=3)
    assert sorted(seen) == [f"DOC_00{i}" for i in range(1, 5)]


def test_observed_concurrency_reaches_the_worker_count(four_docs, monkeypatch):
    live = 0
    peak = 0
    lock = threading.Lock()

    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return {"status": "success"}
    _install(monkeypatch, fake)

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=4)
    assert peak >= 2, f"documents never overlapped (peak={peak})"


def test_sequential_and_parallel_produce_the_same_results(four_docs, monkeypatch):
    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        return {"status": "success", "pages": int(doc_id[-1])}
    _install(monkeypatch, fake)

    one = rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=1)
    many = rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=4)
    assert one["documents"] == many["documents"]


# -------------------------------------------------------------- containment --

def test_a_leak_blocked_document_does_not_stop_the_others(four_docs, monkeypatch):
    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        if doc_id == "DOC_002":
            raise rds.redact_document_mod.RedactionLeakError("residual PII on page 4")
        return {"status": "success"}
    _install(monkeypatch, fake)

    out = rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=4)

    done = {d["doc_id"]: d["status"] for d in out["documents"]}
    assert done["DOC_002"] == "error"
    assert all(done[f"DOC_00{i}"] == "success" for i in (1, 3, 4))


def test_a_blocked_document_still_fails_the_step(four_docs, monkeypatch):
    """Containment must not become "and report success"."""
    def fake(case_id, doc_id, held_by, run_id, redactor, **kw):
        if doc_id == "DOC_003":
            raise RuntimeError("cross-validation gate refused the document")
        return {"status": "success"}
    _install(monkeypatch, fake)

    out = rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=4)
    assert out["status"] == "partial"
    assert [b["doc_id"] for b in out["blocked"]] == ["DOC_003"]
    assert "RuntimeError" in next(
        d["error"] for d in out["documents"] if d["doc_id"] == "DOC_003")


# ------------------------------------------------------------------- shape --

def test_a_case_with_nothing_to_redact_succeeds(monkeypatch):
    monkeypatch.setattr(rds._dao, "read_contract_data",
                        lambda c, n: _manifest(
                            _doc("DOC_001", redacted_text_path="x.md")))
    out = rds.run_redaction_stage("CASE_911", "tester", "RUN_1")
    assert out["status"] == "success"
    assert out["documents"] == []


def test_a_missing_manifest_fails_rather_than_reporting_success(monkeypatch):
    monkeypatch.setattr(rds._dao, "read_contract_data", lambda c, n: None)
    out = rds.run_redaction_stage("CASE_911", "tester", "RUN_1")
    assert out["status"] == "failed"
    assert "document_manifest" in out["error"]


def test_the_redactor_comes_from_redact_documents_own_selector(four_docs, monkeypatch):
    """The PII-free-class routing rule must have exactly one implementation."""
    built = []
    # **kw, not a fixed parameter list: the driver also forwards
    # skip_redaction, and a stub that pins today's exact signature turns any
    # future keyword into a silent no-call (the failure looks like "the
    # selector was never used", which is the opposite of what happened).
    monkeypatch.setattr(
        rds.redact_document_mod, "_redactor_for",
        lambda case_id, doc_id, provider, model, **kw: built.append(
            (doc_id, provider, model)) or object())
    _install(monkeypatch,
             lambda *a, **kw: {"status": "success"})

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=1,
                            provider_name="openai-api", model="gpt-x")

    assert sorted(built) == [(f"DOC_00{i}", "openai-api", "gpt-x") for i in range(1, 5)]


def test_skip_redaction_reaches_the_selector(four_docs, monkeypatch):
    """The dev switch must arrive at the one place that picks the redactor.

    Checkpoint 2 runs twice per case (top-level documents, then the split
    children), so a switch that reached only one of them would redact half the
    case under a different policy.
    """
    seen = []
    monkeypatch.setattr(
        rds.redact_document_mod, "_redactor_for",
        lambda case_id, doc_id, provider, model, *, skip_redaction=None:
            seen.append(skip_redaction) or object())
    _install(monkeypatch, lambda *a, **kw: {"status": "success"})

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=1,
                            skip_redaction=True)
    assert seen == [True] * 4, "every document must get the same policy"


def test_provider_defaults_to_redact_documents_own_default(four_docs, monkeypatch):
    built = []
    monkeypatch.setattr(
        rds.redact_document_mod, "_redactor_for",
        lambda case_id, doc_id, provider, model, **kw: built.append(provider) or object())
    _install(monkeypatch, lambda *a, **kw: {"status": "success"})

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=1)
    assert set(built) == {rds.redact_document_mod.DEFAULT_REDACTION_PROVIDER}


def test_page_workers_is_passed_through_to_each_document(four_docs, monkeypatch):
    seen = []
    _install(monkeypatch,
             lambda *a, max_workers=None, **kw: seen.append(max_workers)
             or {"status": "success"})

    rds.run_redaction_stage("CASE_911", "tester", "RUN_1", doc_workers=1,
                            page_workers=2)
    assert seen == [2, 2, 2, 2]
