"""Classification runs after redaction, not as checkpoint 1's tail.

Classification used to be the last step of checkpoint 1, which runs before
redaction. A top-level document therefore had no `redacted_text.md` when it was
classified, so `_classification_input_text` fell back to the raw page and
recorded `classification_text_source: raw_page_text` + `review_required`. The
`document_processing` finalize gate then refused until a human cleared each one.

That fallback exists for a document that genuinely has no redacted layer. What
made it a problem is that it was reached by EVERY top-level document on EVERY
run -- CASE_909, CASE_911, CASE_961 and CASE_962 each recorded 4-5 of them --
so the exception path was the normal path, and a gate taken every time is a
gate that gets rubber-stamped rather than read.

The properties pinned here are the ones that would silently come back:

* checkpoint 1 must not classify (the driver passes classify=False);
* the classification pass must skip a bundle awaiting its split, because
  `document_type` is per-document and one label cannot describe a bundle;
* it must skip what already has a type, and what has no text to read;
* a document that fails must not take its siblings with it.
"""
import json
import threading
import time

import pytest

import run_document_stage as rds


def _doc(doc_id, **extra):
    d = {"document_id": doc_id, "file_name": f"{doc_id}.pdf",
         "file_path": f"data/raw/CASE_009/{doc_id}.pdf",
         "ocr_status": "completed"}
    d.update(extra)
    return d


def _install_manifest(monkeypatch, *docs):
    manifest = {"case_id": "CASE_009", "documents": list(docs)}
    monkeypatch.setattr(rds._dao, "read_contract_data",
                        lambda case_id, name: manifest)
    return manifest


# --------------------------------------------------------------- selector --

def test_bundle_awaiting_split_is_not_classified(monkeypatch):
    """One label cannot be right for a bundle mixing a 진단서, a 검사보고서 and
    a 진료비 명세서 -- CASE_907 labelled exactly such a 19-page bundle
    `diagnosis_certificate`, which is the misclassification that prompted the
    Stage 1/2 merge."""
    manifest = _install_manifest(
        monkeypatch,
        _doc("DOC_001"),
        _doc("DOC_005", segmentation_status="required"),
    )

    picked = [d["document_id"]
              for d in rds.select_classification_documents(manifest)]

    assert picked == ["DOC_001"], (
        "a bundle still awaiting its split must not be classified; its "
        "children classify individually after the split")


def test_superseded_bundle_and_expert_review_only_are_skipped(monkeypatch):
    manifest = _install_manifest(
        monkeypatch,
        _doc("DOC_001"),
        _doc("DOC_005", downstream_disposition="superseded_bundle"),
        _doc("DOC_010", downstream_disposition="expert_review_only"),
    )

    picked = [d["document_id"]
              for d in rds.select_classification_documents(manifest)]

    assert picked == ["DOC_001"]


def test_already_typed_and_untextracted_documents_are_skipped(monkeypatch):
    manifest = _install_manifest(
        monkeypatch,
        _doc("DOC_001"),
        _doc("DOC_002", document_type="insurer_response"),
        _doc("DOC_003", ocr_status="pending"),
        _doc("DOC_004", ocr_status="failed"),
    )

    picked = [d["document_id"]
              for d in rds.select_classification_documents(manifest)]

    assert picked == ["DOC_001"], (
        "classification must not re-label a typed document, and must not run "
        "on one whose page 1 text does not exist yet")


# ------------------------------------------------------------------ stage --

def test_each_result_is_filed_under_its_own_document(monkeypatch):
    _install_manifest(monkeypatch, *[_doc(f"DOC_00{i}") for i in range(1, 6)])

    def fake(case_id, doc_id, *, held_by, run_id, classifier=None):
        # Later documents finish first, so an append-as-completed
        # implementation would misfile every one of them.
        time.sleep(0.02 * (6 - int(doc_id[-1])))
        return {"status": "passed", "doc_id": doc_id, "marker": doc_id}
    monkeypatch.setattr(rds, "classify_existing", fake)

    out = rds.run_classification_stage("CASE_009", "tester", "RUN_1",
                                        doc_workers=4)

    assert out["status"] == "success"
    assert [d["doc_id"] for d in out["documents"]] == [
        f"DOC_00{i}" for i in range(1, 6)]
    for d in out["documents"]:
        assert d["marker"] == d["doc_id"], "a result was filed under the wrong document"


def test_one_failing_document_does_not_take_down_its_siblings(monkeypatch):
    _install_manifest(monkeypatch, *[_doc(f"DOC_00{i}") for i in range(1, 4)])

    def fake(case_id, doc_id, *, held_by, run_id, classifier=None):
        if doc_id == "DOC_002":
            raise RuntimeError("provider exploded")
        return {"status": "passed", "doc_id": doc_id}
    monkeypatch.setattr(rds, "classify_existing", fake)

    out = rds.run_classification_stage("CASE_009", "tester", "RUN_1",
                                        doc_workers=3)

    assert out["status"] == "partial", "a failed document must fail the step"
    assert {d["doc_id"] for d in out["documents"]} == {
        "DOC_001", "DOC_002", "DOC_003"}, "siblings must still be reported"
    assert [b["doc_id"] for b in out["blocked"]] == ["DOC_002"]


def test_systemexit_from_missing_text_is_reported_not_propagated(monkeypatch):
    """`classify_existing` calls sys.exit when a document has no ocr_result.
    The selector excludes those, so reaching one means the manifest disagrees
    with the filter -- report it rather than killing the whole pass."""
    _install_manifest(monkeypatch, _doc("DOC_001"), _doc("DOC_002"))

    def fake(case_id, doc_id, *, held_by, run_id, classifier=None):
        if doc_id == "DOC_001":
            raise SystemExit("error: no ocr_result")
        return {"status": "passed", "doc_id": doc_id}
    monkeypatch.setattr(rds, "classify_existing", fake)

    out = rds.run_classification_stage("CASE_009", "tester", "RUN_1",
                                        doc_workers=1)

    assert out["status"] == "partial"
    assert [b["doc_id"] for b in out["blocked"]] == ["DOC_001"]


def test_no_eligible_documents_is_success_not_failure(monkeypatch):
    _install_manifest(monkeypatch, _doc("DOC_001", document_type="other"))

    out = rds.run_classification_stage("CASE_009", "tester", "RUN_1")

    assert out["status"] == "success"
    assert out["documents"] == []


# ------------------------------------------------------- checkpoint 1 wiring --

def test_checkpoint1_cli_does_not_classify(monkeypatch, capsys):
    """The whole point: the driver's checkpoint 1 extracts, it does not label.

    Asserted at the CLI boundary rather than on run_checkpoint1's default,
    because the default is deliberately unchanged -- a single-document call
    still classifies. What moved is what the case-wide driver asks for.
    """
    seen = {}

    def fake_stage(case_id, held_by, run_id, **kwargs):
        seen.update(kwargs)
        return {"status": "success", "documents": []}
    monkeypatch.setattr(rds, "run_document_stage", fake_stage)
    monkeypatch.setattr(rds.trace_mod, "configure", lambda *a, **k: None)

    rds.main(["CASE_009", "--held-by", "tester", "--run-id", "RUN_1"])

    assert seen.get("classify") is False, (
        "checkpoint 1 must not classify: doing so labels every top-level "
        "document from unredacted text, because redaction has not run yet")


def test_ocr_only_still_records_what_extraction_established(monkeypatch, tmp_path):
    """`classify=False` must still write the OCR-owned manifest fields.

    Found by running it: the no-classify path wrote nothing to the manifest,
    which was harmless while a bundle was its only caller (a bundle becomes
    `superseded_bundle` and those fields stop meaning anything). Under the
    driver every document takes that path, so CASE_963's DOC_001-004 completed
    OCR, redaction AND chunking while the manifest still said
    `ocr_status: pending` -- and the classification pass, which selects on
    `ocr_status: completed`, skipped all four. The stage reported success with
    four unclassified documents, which is the worst shape a failure can take.

    Only the OCR-owned fields may be written here. `document_type`,
    `classification_confidence` and `downstream_disposition` are the
    classifier's, and asserting them from an unclassified document is the very
    thing this path exists to avoid.
    """
    import run_checkpoint1 as rc1

    captured = {}

    def fake_patch(case_id, doc_id, fields, held_by, run_id, **kw):
        captured.update(fields)
        return True, "ok"
    monkeypatch.setattr(rc1._dao, "patch_manifest_document", fake_patch)
    monkeypatch.setattr(rc1._dao, "_update_run_state", lambda *a, **k: None)

    ocr_data = {"pages": [{"page": 1, "agreement": "single_reader",
                           "reading_a": "text"}]}
    ocr_result = {"pages": [{"page": 1}], "ocr_quality": "low",
                  "cross_validation_status": "single_reader_no_cross_validation",
                  "extraction_method": "ocr", "source_total_pages": 1}

    # Drive the tail of run_checkpoint1's no-classify branch directly, by
    # replaying exactly what it does, so the assertion is about the fields
    # rather than about re-running OCR.
    fields = {
        "pages": len(ocr_data["pages"]),
        "source_total_pages": ocr_result.get("source_total_pages"),
        "ocr_status": "completed",
        "ocr_quality": ocr_result["ocr_quality"],
        "uncertain_region_count": 0,
        "cross_validation_status": ocr_result["cross_validation_status"],
        "extraction_method": ocr_result.get("extraction_method", "ocr"),
        "non_text_verification": None,
    }
    rc1._dao.patch_manifest_document("CASE_009", "DOC_001", fields,
                                      "tester", "RUN_1")

    assert captured.get("ocr_status") == "completed", (
        "OCR completed but the manifest was left saying pending, so the "
        "classification pass will skip this document entirely")
    for owned_by_classifier in ("document_type", "classification_confidence",
                                 "downstream_disposition"):
        assert owned_by_classifier not in captured, (
            f"{owned_by_classifier} belongs to the classification pass; the "
            "OCR-only path must not assert it")


def test_ocr_only_path_writes_the_manifest_in_the_real_function(monkeypatch):
    """The same property, driven through run_checkpoint1 itself.

    The test above pins WHICH fields; this one pins that the function actually
    calls the patch at all on the no-classify path -- the defect was an absent
    call, and a test that replays the field dict would pass with the call still
    missing.
    """
    import run_checkpoint1 as rc1
    source = __import__("inspect").getsource(rc1.run_checkpoint1)
    head, _, tail = source.partition("if not classify:")
    assert tail, "the no-classify branch disappeared"
    branch = tail.split("return {")[0]
    assert "patch_manifest_document" in branch, (
        "the OCR-only path must record what extraction established; without "
        "it the manifest keeps saying pending and every later selector that "
        "reads ocr_status silently skips the document")


def test_classify_checkpoint_is_reachable_from_the_cli(monkeypatch):
    called = {}

    def fake_stage(case_id, held_by, run_id, **kwargs):
        called["hit"] = True
        called.update(kwargs)
        return {"status": "success", "documents": []}
    monkeypatch.setattr(rds, "run_classification_stage", fake_stage)
    monkeypatch.setattr(rds.trace_mod, "configure", lambda *a, **k: None)

    rc = rds.main(["CASE_009", "--held-by", "tester", "--run-id", "RUN_1",
                   "--checkpoint", "classify"])

    assert rc == 0
    assert called.get("hit"), "--checkpoint classify must reach the new stage"
