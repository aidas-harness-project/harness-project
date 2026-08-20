"""run_checkpoint1.py -- the checkpoint-1 automation wrapper (OCR + classify,
stopping at a P8 disagreement; resolve_from_raw_ocr() continues past one
once a human decides). Provider calls are mocked -- these tests never shell
out to a real CLI or call an external API.
"""
import hashlib
import json

import pytest

import dao
import run_checkpoint1 as rc1
from llm_providers import ProviderResult


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "DATA", data)
    monkeypatch.setattr(rc1, "ROOT", tmp_path)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)
    return tmp_path


def _seed_manifest(tmp_path, case_id, doc_id):
    out_dir = tmp_path / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": case_id, "created_at": dao.now_iso(),
        "documents": [{"document_id": doc_id, "file_name": f"{doc_id}.pdf", "file_path": f"data/raw/{case_id}/{doc_id}.pdf",
                       "file_format": "pdf", "file_size_bytes": 1000, "ocr_status": "pending",
                       "segmentation_status": "not_required",
                       "segmentation_reviewed_by": "fixture reviewer",
                       "segmentation_reviewed_at": dao.now_iso()}],
    })


def _mock_ocr(monkeypatch, pages):
    """pages: list of (reading_a, reading_b, agreement) tuples."""
    monkeypatch.setattr(
        rc1, "source_pdf_page_count", lambda _path: len(pages))

    def fake_run_ocr(case_id, doc_id, pdf_path, progress=None, **kwargs):
        return {"document_path": str(pdf_path), "pages": [
            {"page": i, "reading_a": a, "reading_b": b, "agreement": agree,
             "disagreement_details": [] if agree == "agreed" else ["DISAGREE: mock"]}
            for i, (a, b, agree) in enumerate(pages, start=1)
        ]}
    monkeypatch.setattr(rc1, "run_ocr", fake_run_ocr)


def _mock_classify(monkeypatch, doc_type="insurer_response", label="보험사 회신"):
    def fake_classify(text, classifier=None):
        return {"predicted_document_type": doc_type, "document_type_label": label,
                "confidence": 0.9, "quote": text[:20]}
    monkeypatch.setattr(rc1, "classify_document", fake_classify)


def _enable_medical_routing(monkeypatch):
    config = json.loads(json.dumps(rc1._medical_routing.load_routing_config()))
    config["behavior_enabled"] = True
    monkeypatch.setattr(rc1._medical_routing, "load_routing_config", lambda: config)
    return config


class FakeClassifier:
    provider_name = "openai-api"
    model_name = "gpt-test"

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def classify_document(self, prompt, prompt_version):
        self.prompts.append((prompt, prompt_version))
        return ProviderResult(self.provider_name, self.model_name, prompt_version, self.response)


def test_an_unreviewed_pdf_is_processed_rather_than_blocked(tmp_path, monkeypatch):
    """Reading a bundle is the point of the inverted order, not a violation.

    Checkpoint 1 used to refuse any PDF whose bundle question a human had not
    answered. That protected an order where segmentation ran first and an
    unsplit bundle would be classified as one document. With OCR first, whether
    a PDF is a bundle is something segmentation ANSWERS from the text this run
    produces -- it cannot be a precondition for producing it.
    """
    out_dir = tmp_path / "outputs" / "CASE_009"
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": "CASE_009", "created_at": dao.now_iso(),
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 1000, "ocr_status": "pending",
            "segmentation_status": "pending_review",
        }],
    })
    _mock_ocr(monkeypatch, [("page one", "page one b", "agreed")])
    _mock_classify(monkeypatch)

    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260721_001")

    assert result["status"] == "passed"
    assert (out_dir / "ocr_result_DOC_001.json").exists()


def test_a_superseded_bundle_is_still_refused(tmp_path, monkeypatch):
    """The one refusal that remains, and it applies in both modes.

    After a split the parent represents nothing its children do not; reading it
    again would duplicate every page. Unlike the bundle question, this is not a
    judgement anyone has to make in advance -- the split itself records it.
    """
    out_dir = tmp_path / "outputs" / "CASE_009"
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": "CASE_009", "created_at": dao.now_iso(),
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 1000, "ocr_status": "not_applicable",
            "segmentation_status": "completed",
            "downstream_disposition": "superseded_bundle",
        }],
    })
    monkeypatch.setattr(
        rc1, "run_ocr",
        lambda *a, **k: pytest.fail("a superseded bundle must never be re-read"))

    for classify in (True, False):
        result = rc1.run_checkpoint1(
            "CASE_009", "DOC_001", "missing.pdf", "tester", "RUN_20260721_001",
            classify=classify,
            reader_a=object(), reader_b=object(), comparator=object(),
            classifier=object())
        assert result["status"] == "blocked_segmentation"
    assert not (out_dir / "ocr_result_DOC_001.json").exists()


def test_page_range_pdf_falls_back_to_pypdf(tmp_path, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    import builtins

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fitz":
            raise ImportError("fitz missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    pdf_path = tmp_path / "source.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as f:
        writer.write(f)

    with rc1._page_range_pdf(pdf_path, "CASE_009", "DOC_001", 2, 2) as subset_path:
        assert subset_path != pdf_path
        assert subset_path.exists()
        reader = pypdf.PdfReader(str(subset_path))
        assert len(reader.pages) == 1

    assert not subset_path.exists()


def test_all_agreed_passes_through_to_classification(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("page one text", "page one text b", "agreed"),
                            ("page two text", "page two text b", "agreed")])
    _mock_classify(monkeypatch)

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert result["status"] == "passed"
    assert result["document_type"] == "insurer_response"
    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    assert ocr_result["cross_validation_status"] == "agreed"
    classification = json.loads((tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").read_text(encoding="utf-8"))
    assert classification["predicted_document_type"] == "insurer_response"
    manifest = json.loads((tmp_path / "outputs" / "CASE_009" / "document_manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["ocr_status"] == "completed"
    assert manifest["documents"][0]["document_type"] == "insurer_response"
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["status"] == "in_progress", \
        "one document's checkpoint 1 must not pass the whole document_processing stage"


def test_checkpoint1_provider_backed_classification_without_claude_cli(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("provider page text", "provider page text b", "agreed")])
    classifier = FakeClassifier(
        '{"predicted_document_type": "medical_record", "document_type_label": "의무기록", '
        '"confidence": 0.88, "quote": "provider page text"}'
    )

    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001",
        classifier=classifier,
    )

    assert result["status"] == "passed"
    assert result["document_type"] == "medical_record"
    assert classifier.prompts[0][1] == rc1.CLASSIFICATION_PROMPT_VERSION
    classification = json.loads(
        (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").read_text(encoding="utf-8")
    )
    assert classification["predicted_document_type"] == "medical_record"
    assert classification["model_info"]["model_name"] == "openai-api:gpt-test"
    assert classification["model_info"]["provider_name"] == "openai-api"


def test_classifier_defaults_to_comparator_provider(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("page text", "page text b", "agreed")])
    comparator_and_classifier = FakeClassifier(
        '{"predicted_document_type": "receipt", "document_type_label": "영수증", '
        '"confidence": 0.77, "quote": "page text"}'
    )

    # Dual-read explicitly: the comparator only EXISTS on that path (under
    # the PoC's single-reader default reader_b and the comparator are never
    # built), so "the classifier falls back to the comparator's provider" is
    # a dual-read property and the test has to ask for it.
    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001",
        comparator=comparator_and_classifier, single_reader=False,
    )

    assert result["status"] == "passed"
    assert result["document_type"] == "receipt"
    assert len(comparator_and_classifier.prompts) == 1


def test_classifier_model_override_builds_new_provider(monkeypatch):
    comparator = FakeClassifier(
        '{"predicted_document_type": "receipt", "document_type_label": "영수증", '
        '"confidence": 0.77, "quote": "page text"}'
    )
    built = []

    def fake_build_provider(config, **kwargs):
        built.append(config)
        return FakeClassifier("{}")

    monkeypatch.setattr(rc1, "build_provider", fake_build_provider)

    provider = rc1.build_classifier_provider(
        comparator_provider=comparator,
        env={"HARNESS_CLASSIFIER_MODEL": "classifier-only-model"},
    )

    assert provider is not comparator
    assert built[0].provider_name == "openai-api"
    assert built[0].model_name == "classifier-only-model"


def test_explicit_classifier_provider_does_not_inherit_different_comparator_model(monkeypatch):
    comparator = FakeClassifier(
        '{"predicted_document_type": "receipt", "document_type_label": "영수증", '
        '"confidence": 0.77, "quote": "page text"}'
    )
    built = []

    def fake_build_provider(config, **kwargs):
        built.append(config)
        return FakeClassifier("{}")

    monkeypatch.setattr(rc1, "build_provider", fake_build_provider)

    rc1.build_classifier_provider(
        classifier_provider_name="fixture",
        comparator_provider=comparator,
        env={},
    )

    assert built[0].provider_name == "fixture"
    assert built[0].model_name is None


def test_agreed_pages_written_to_processed_layer(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("real page text", "real page text b", "agreed")])
    _mock_classify(monkeypatch)

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == "real page text"


def test_disagreement_blocks_and_does_not_classify(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("page one A", "page one B", "agreed"),
                            ("page two A", "page two B", "disagreed")])
    classify_called = []
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: classify_called.append(text) or {})

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert result["status"] == "blocked_disagreement"
    assert result["disagreed_pages"] == [2]
    assert classify_called == [], "must not classify while a disagreement is unresolved"
    assert not (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").exists()
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["status"] == "failed", "run-state must reflect the real block, not stay untouched"


def test_blocked_run_resets_manifest_instead_of_leaving_it_stale(tmp_path, monkeypatch):
    """Real bug, found by actually running the scenario matrix against a
    forked case that had PREVIOUSLY passed: without this reset, a fresh
    run that newly finds a disagreement would leave document_manifest.json
    showing the OLD completed/passed values, directly contradicting the
    fresh ocr_result.json that says disagreed_pending_review. Not a
    fork-specific issue -- the same staleness would hit any genuine re-run
    that newly fails after a prior success."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    manifest_path = tmp_path / "outputs" / "CASE_009" / "document_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"][0].update({
        "ocr_status": "completed", "ocr_quality": "high", "uncertain_region_count": 0,
        "cross_validation_status": "agreed", "redacted_text_path": "data/processed/CASE_009/DOC_001/redacted_text.md",
        "document_type": "insurer_response", "classification_confidence": 0.9,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # Simulate the fork source's prior legitimate finalize (snapshot + passed).
    dao._finalize_stage("CASE_009", "RUN_OLD", "document_processing", "tester")

    _mock_ocr(monkeypatch, [("A", "B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_002")

    fresh_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc = fresh_manifest["documents"][0]
    assert doc["ocr_status"] == "failed"
    assert doc["cross_validation_status"] == "disagreed_pending_review"
    assert doc["redacted_text_path"] is None, "stale redaction path must not survive a fresh extraction failure"
    assert doc["document_type"] is None
    assert doc["classification_confidence"] is None
    state = dao.load_run_state("CASE_009")
    assert state["stages"][-1]["status"] == "failed"


def test_disagreed_page_has_no_text_path_agreed_page_does(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("agreed text", "agreed text b", "agreed"),
                            ("A version", "B version", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    pages = {p["page"]: p for p in ocr_result["pages"]}
    assert pages[1]["text_path"] is not None
    assert pages[2]["text_path"] is None


def test_raw_ocr_scratch_saved_when_blocked(tmp_path, monkeypatch):
    """This is what lets a *separate, later* process resolve the
    disagreement without re-running real OCR -- reading_a's full text
    isn't retained in ocr_result.json itself (only reading_b is)."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("the real reading_a text", "a different reading_b text", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    raw_path = tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json"
    assert raw_path.exists()
    assert result["raw_ocr_path"] == str(raw_path)
    saved = json.loads(raw_path.read_text(encoding="utf-8"))
    assert saved["pages"][0]["reading_a"] == "the real reading_a text"


def test_resolve_from_raw_ocr_completes_a_single_page_document(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("A reading", "B reading", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})

    blocked = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"

    ocr_data = json.loads((tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json").read_text(encoding="utf-8"))
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")

    result = rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_b",
                                       resolved_by="Dev", note="verified against raw image",
                                       held_by="tester", run_id="RUN_20260713_002")

    assert result["status"] == "passed"
    assert result["document_type"] == "medical_record"
    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == "B reading"
    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    assert ocr_result["cross_validation_status"] == "disagreed_resolved"
    resolution = ocr_result["pages"][0]["cross_validation"]["resolution"]
    assert resolution == {"chosen_reading": "reading_b", "resolved_by": "Dev",
                           "resolved_at": resolution["resolved_at"], "note": "verified against raw image"}


def test_resolve_from_raw_ocr_partial_when_multiple_disagreements(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("p1 A", "p1 B", "disagreed"), ("p2 A", "p2 B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})

    blocked = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    ocr_data = json.loads((tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json").read_text(encoding="utf-8"))

    result = rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_a",
                                       resolved_by="Dev", note="n", held_by="tester", run_id="RUN_20260713_002")

    assert result["status"] == "partially_resolved"
    assert result["still_unresolved"] == [2]
    assert not (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").exists()


def test_resolve_from_raw_ocr_accepts_human_corrected_transcription(
        tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("wrong A", "wrong B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})
    blocked = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"

    ocr_data = json.loads(
        (tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json").read_text(
            encoding="utf-8"))
    corrected_text = "Human-verified complete page transcription"
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")

    result = rc1.resolve_from_raw_ocr(
        "CASE_009", "DOC_001", ocr_data, page=1, chosen_reading=None,
        corrected_text=corrected_text, resolved_by="Reviewer",
        note="Neither automated reading was correct; verified against the source page.",
        held_by="tester", run_id="RUN_20260713_002")

    assert result["status"] == "passed"
    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == corrected_text
    ocr_result = json.loads(
        (tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(
            encoding="utf-8"))
    resolution = ocr_result["pages"][0]["cross_validation"]["resolution"]
    assert resolution["chosen_reading"] == "human_corrected"
    assert resolution["corrected_text_sha256"] == hashlib.sha256(
        corrected_text.encode("utf-8")).hexdigest()
    assert ocr_data["pages"][0]["reading_a"] == "wrong A"
    assert ocr_data["pages"][0]["reading_b"] == "wrong B"


def test_cli_resolve_disagreement_reads_human_corrected_text_file(
        tmp_path, monkeypatch, capsys):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("wrong A", "wrong B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})
    blocked = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"
    corrected_path = tmp_path / "corrected-page.md"
    corrected_path.write_text("verified corrected page", encoding="utf-8")
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")

    rc1.main([
        "resolve-disagreement", "CASE_009", "DOC_001",
        "--page", "1", "--corrected-text-file", str(corrected_path),
        "--resolved-by", "Reviewer", "--note", "verified full page",
        "--held-by", "document-pipeline", "--run-id", "RUN_20260713_002",
        "--classifier-provider", "fixture",
    ])

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "passed"
    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == "verified corrected page"


def test_resolve_as_non_text_preserves_disagreement_and_writes_no_text(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_010")
    _mock_ocr(monkeypatch, [("reader refused", "no transcribable text", "disagreed"),
                            ("reader refused", "no transcribable text", "disagreed")])
    blocked = rc1.run_checkpoint1(
        "CASE_009", "DOC_010", "fake.pdf", "tester", "RUN_20260713_001"
    )
    assert blocked["status"] == "blocked_disagreement"

    result = rc1.resolve_as_non_text(
        "CASE_009", "DOC_010",
        verified_by="Pyun", reviewer_role="의사",
        note="Human verified both pages are visual evidence with no faithful text transcription.",
        held_by="document-pipeline", run_id="RUN_20260713_002",
    )

    assert result["status"] == "non_text_verified"
    assert result["downstream_disposition"] == "expert_review_only"
    ocr_result = json.loads(
        (tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_010.json").read_text(encoding="utf-8")
    )
    assert ocr_result["extraction_method"] == "non_text_image"
    assert ocr_result["ocr_status"] == "not_applicable"
    assert ocr_result["cross_validation_status"] == "non_text_verified"
    assert ocr_result["review_required"] is True
    assert all(page["text_path"] is None for page in ocr_result["pages"])
    assert all(page["cross_validation"]["agreement"] == "disagreed" for page in ocr_result["pages"])
    assert all(page["non_text_verification"]["downstream_disposition"] == "expert_review_only"
               for page in ocr_result["pages"])
    assert not (tmp_path / "data" / "processed" / "CASE_009" / "DOC_010" / "page_001.md").exists()
    assert not (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_010.json").exists()

    manifest = json.loads(
        (tmp_path / "outputs" / "CASE_009" / "document_manifest.json").read_text(encoding="utf-8")
    )["documents"][0]
    assert manifest["ocr_status"] == "not_applicable"
    assert manifest["document_type"] == "other"
    assert manifest["downstream_disposition"] == "expert_review_only"
    assert manifest["redacted_text_path"] is None
    assert dao.load_run_state("CASE_009")["stages"][0]["status"] == "failed", \
        "one document resolution must not mark the whole document_processing stage passed"


def test_resolve_as_non_text_refuses_to_invalidate_existing_page_text(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_010")
    _mock_ocr(monkeypatch, [("valid text", "valid text", "agreed"),
                            ("refusal", "no text", "disagreed")])
    rc1.run_checkpoint1("CASE_009", "DOC_010", "fake.pdf", "tester", "RUN_20260713_001")

    with pytest.raises(SystemExit):
        rc1.resolve_as_non_text(
            "CASE_009", "DOC_010",
            verified_by="Pyun", reviewer_role="의사", note="whole document is visual",
            held_by="document-pipeline", run_id="RUN_20260713_002",
        )


def test_cli_resolve_non_text_is_reachable(tmp_path, monkeypatch, capsys):
    _seed_manifest(tmp_path, "CASE_009", "DOC_010")
    _mock_ocr(monkeypatch, [("refusal", "no text", "disagreed")])
    rc1.run_checkpoint1("CASE_009", "DOC_010", "fake.pdf", "tester", "RUN_20260713_001")

    rc1.main([
        "resolve-non-text", "CASE_009", "DOC_010",
        "--verified-by", "Pyun", "--reviewer-role", "의사",
        "--note", "Human verified this whole document is non-text visual evidence.",
        "--held-by", "document-pipeline", "--run-id", "RUN_20260713_002",
    ])

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "non_text_verified"
    assert out["reviewer_role"] == "의사"


def test_resolve_from_raw_ocr_rejects_bad_chosen_reading(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    ocr_data = {"pages": [{"page": 1, "reading_a": "a", "reading_b": "b"}]}
    with pytest.raises(SystemExit):
        rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_c",
                                  resolved_by="Dev", note="n", held_by="tester", run_id="RUN_20260713_001")


def test_cli_resolve_disagreement_loads_scratch_dump_and_resolves(tmp_path, monkeypatch, capsys):
    """The resolve-disagreement subcommand must recover both readings from the
    _ocr_scratch dump and drive resolve_from_raw_ocr end-to-end -- i.e. the
    already-tested resolver is actually reachable from the CLI."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("A reading", "B reading", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})
    blocked = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"

    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")
    rc1.main([
        "resolve-disagreement", "CASE_009", "DOC_001",
        "--page", "1", "--chosen-reading", "reading_b",
        "--resolved-by", "Pyun", "--note", "refusal on reading_a; reading_b is the faithful transcription",
        "--held-by", "document-pipeline", "--run-id", "RUN_20260713_002",
    ])

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "passed"
    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    assert ocr_result["cross_validation_status"] == "disagreed_resolved"
    assert ocr_result["pages"][0]["cross_validation"]["resolution"]["resolved_by"] == "Pyun"


def test_cli_resolve_disagreement_uses_explicit_classifier_provider(
        tmp_path, monkeypatch, capsys):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("A reading", "B reading", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {})
    blocked = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"

    classifier = object()
    provider_args = {}

    def fake_build_classifier_provider(**kwargs):
        provider_args.update(kwargs)
        return classifier

    monkeypatch.setattr(rc1, "build_classifier_provider", fake_build_classifier_provider)

    def fake_classify(text, selected_classifier=None):
        assert selected_classifier is classifier
        return {
            "predicted_document_type": "medical_record",
            "document_type_label": "의무기록",
            "confidence": 0.9,
            "quote": text[:20],
        }

    monkeypatch.setattr(rc1, "classify_document", fake_classify)
    rc1.main([
        "resolve-disagreement", "CASE_009", "DOC_001",
        "--page", "1", "--chosen-reading", "reading_b",
        "--resolved-by", "Pyun", "--note", "verified against the source page",
        "--held-by", "document-pipeline", "--run-id", "RUN_20260713_002",
        "--classifier-provider", "codex-cli", "--classifier-model", "gpt-test",
    ])

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "passed"
    assert provider_args == {
        "classifier_provider_name": "codex-cli",
        "classifier_model": "gpt-test",
    }


def test_cli_resolve_disagreement_errors_when_scratch_dump_missing(tmp_path):
    with pytest.raises(SystemExit):
        rc1.main([
            "resolve-disagreement", "CASE_404", "DOC_001",
            "--page", "1", "--chosen-reading", "reading_a",
            "--resolved-by", "Pyun", "--note", "n",
            "--held-by", "document-pipeline", "--run-id", "RUN_1",
        ])


def test_cli_legacy_positional_form_still_routes_to_run(tmp_path, monkeypatch, capsys):
    """Backward compatibility: the old `CASE DOC PDF --held-by ... --run-id ...`
    form (no subcommand token) must still run checkpoint 1 unchanged."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("same", "same", "agreed")])
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")
    rc1.main(["CASE_009", "DOC_001", "fake.pdf", "--held-by", "tester", "--run-id", "RUN_20260713_001"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "passed"


def test_classify_document_parses_provider_response():
    classifier = FakeClassifier(
        '{"predicted_document_type": "insurer_response", "document_type_label": "회신", '
        '"confidence": 0.95, "quote": "sample"}'
    )

    result = rc1.classify_document("some document text", classifier)

    assert result["predicted_document_type"] == "insurer_response"
    assert result["confidence"] == 0.95
    assert result["_provider_metadata"]["provider_name"] == "openai-api"
    assert classifier.prompts[0][1] == rc1.CLASSIFICATION_PROMPT_VERSION


def test_classify_document_fails_loud_on_unparseable_response():
    classifier = FakeClassifier("not json at all")

    with pytest.raises(SystemExit):
        rc1.classify_document("some text", classifier)


def test_classify_document_rejects_unknown_document_type():
    classifier = FakeClassifier('{"predicted_document_type": "not_a_real_type", "confidence": 0.9, "quote": "x"}')

    with pytest.raises(SystemExit):
        rc1.classify_document("some text", classifier)


# --- classification inheritance for text-anchor policy slices -----------------

def _inherit_case(tmp_path, *, child=None, parent=None, proposal=None):
    """Writes a manifest + proposal on disk and returns the manifest dict."""
    case_id = "CASE_905"
    out = tmp_path / "outputs" / case_id
    out.mkdir(parents=True, exist_ok=True)
    prop = {"review_status": "approved",
            "method": {"mode": "text_anchor"},
            "segments": [{"page_start": 5, "page_end": 6,
                          "provisional_type_label": "구내치료비 추가특별약관"}]}
    prop.update(proposal or {})
    (out / "segmentation_proposal_DOC_003.json").write_text(
        json.dumps(prop, ensure_ascii=False), encoding="utf-8")

    parent_doc = {"document_id": "DOC_003", "file_name": "DOC_003.pdf",
                  "downstream_disposition": "superseded_bundle"}
    parent_doc.update(parent or {})
    child_doc = {"document_id": "DOC_007", "file_name": "DOC_007.pdf",
                 "extraction_method": "embedded_text",
                 "source_file_name": "DOC_003.pdf", "source_page_start": 5,
                 "segmentation_proposal_path":
                     f"outputs/{case_id}/segmentation_proposal_DOC_003.json"}
    child_doc.update(child or {})
    return {"documents": [parent_doc, child_doc]}


def test_inherited_classification_uses_the_split_evidence_not_the_parent_type(isolated_roots):
    """The parent is a superseded bundle and never gets a document_type of its
    own; the proposal's text-anchor title line is what establishes the type."""
    m = _inherit_case(isolated_roots)
    got = rc1.inherited_classification("CASE_905", "DOC_007", m)
    assert got["predicted_document_type"] == "insurance_policy"
    assert got["document_type_label"] == "구내치료비 추가특별약관"
    assert got["_inherited_from"] == "DOC_003"


def test_inherited_classification_accepts_an_ocr_sourced_text_anchor_slice(isolated_roots):
    """Where the TEXT came from does not decide how the BOUNDARY was found.

    This condition used to require extraction_method == 'embedded_text', on the
    reasoning that a vision-OCR'd slice might come from a scan whose boundaries
    are a model's reading. But `mode == 'text_anchor'` already excludes exactly
    that: those boundaries are cut on printed 약관 title lines, never by a
    model. extraction_method was standing in for a question it cannot answer.

    Measured on CASE_112's two policy bundles (323 pages): boundaries derived
    from the OCR-produced page text are IDENTICAL to those derived from the
    embedded text layer -- same 173 boundaries, precision 1.0000 against the
    human-approved baseline for both. A text-anchor slice is therefore the same
    evidence whichever reader produced the text it was cut from.

    P8 is untouched: the slice still carries its own cross-validation. What is
    skipped is only the classifier call.
    """
    m = _inherit_case(isolated_roots, child={"extraction_method": "ocr"})
    got = rc1.inherited_classification("CASE_905", "DOC_007", m)
    assert got["predicted_document_type"] == "insurance_policy"
    assert got["_inherited_from"] == "DOC_003"


def test_inherited_classification_refuses_a_vision_mode_proposal(isolated_roots):
    """Vision boundaries are a model's reading, so a slice from them is
    classified on its own evidence."""
    m = _inherit_case(isolated_roots, proposal={"method": {"mode": "vision_proposal"}})
    assert rc1.inherited_classification("CASE_905", "DOC_007", m) is None


def test_inherited_classification_refuses_an_unapproved_proposal(isolated_roots):
    m = _inherit_case(isolated_roots, proposal={"review_status": "pending"})
    assert rc1.inherited_classification("CASE_905", "DOC_007", m) is None


def test_inherited_classification_refuses_a_non_policy_title(isolated_roots):
    """A slice whose title is not a 약관 heading is not a policy by
    construction -- e.g. a 진단서 bundle cut on some other anchor."""
    m = _inherit_case(isolated_roots, proposal={"segments": [
        {"page_start": 5, "page_end": 6, "provisional_type_label": "진단서"}]})
    assert rc1.inherited_classification("CASE_905", "DOC_007", m) is None


def test_inherited_classification_refuses_an_unsegmented_document(isolated_roots):
    m = _inherit_case(isolated_roots, child={"source_file_name": None,
                                             "segmentation_proposal_path": None})
    assert rc1.inherited_classification("CASE_905", "DOC_007", m) is None


def test_inherited_classification_refuses_a_missing_parent(isolated_roots):
    m = _inherit_case(isolated_roots, child={"source_file_name": "GONE.pdf"})
    assert rc1.inherited_classification("CASE_905", "DOC_007", m) is None


# --------------------------------------------- default downstream disposition --
#
# Normalizing a policy bundle is the expensive obligation (800+ conditions on a
# single 145-page 약관) and nothing downstream consumes its output, so a policy
# document is classified into text_only_no_normalization and promoted to
# automated_text_pipeline only when a case actually disputes it. Every other
# document type is unaffected.

def test_policy_documents_default_to_no_normalization():
    assert rc1.default_disposition("insurance_policy") == \
        "text_only_no_normalization"


@pytest.mark.parametrize("document_type", [
    "insurer_response", "diagnosis_certificate", "medical_record",
    "receipt", "other", None,
])
def test_non_policy_documents_keep_the_full_pipeline(document_type):
    assert rc1.default_disposition(document_type) == "automated_text_pipeline"


def test_both_defaults_are_text_processed():
    """Neither default may exclude the document from text processing -- that is
    expert_review_only's job, and reaching it by classification would silently
    stop the pipeline reading a document it is supposed to read."""
    from policy_completeness import _TEXT_PROCESSED
    for document_type in ("insurance_policy", "insurer_response"):
        assert rc1.default_disposition(document_type) in _TEXT_PROCESSED


# --- pre-segmentation bundle OCR ---------------------------------------------
#
# Running OCR before segmentation lets boundaries be derived from real page
# text instead of a downscaled contact-sheet crop. That inverts what the
# Stage-1 gate must block: the gate exists because document_type is a
# PER-DOCUMENT value and one label cannot be right for a bundle mixing a
# 진단서, a 검사보고서 and an 입퇴원확인서. OCR itself is page-wise and carries
# no such assumption, so it is safe on an unsplit bundle -- classification is
# not. The gate therefore narrows from "no Stage 2 work at all" to "no
# CLASSIFICATION before the split".


def _pending_bundle_manifest(tmp_path, case_id="CASE_009"):
    out_dir = tmp_path / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": case_id, "created_at": dao.now_iso(),
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": f"data/raw/{case_id}/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 1000, "ocr_status": "pending",
            "segmentation_status": "required",
        }],
    })
    return out_dir


def test_bundle_ocr_mode_is_allowed_while_segmentation_is_still_pending(
        tmp_path, monkeypatch):
    """OCR on an unsplit bundle is the whole point of the inverted order.

    Asserted by reaching OCR: the gate is evaluated before any PDF work, so a
    call that gets as far as run_ocr has passed it. The bundle is `required`,
    the exact state that blocks a classifying call.
    """
    _pending_bundle_manifest(tmp_path)
    _mock_ocr(monkeypatch, [("bundle page one", "bundle page one b", "agreed"),
                            ("bundle page two", "bundle page two b", "agreed")])
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("a bundle must never be classified as one document"),
    )

    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260721_001",
        classify=False,
        reader_a=object(), reader_b=object(), comparator=object(),
    )

    assert result["status"] == "bundle_ocr_complete"
    assert result["pages"] == 2
    out_dir = tmp_path / "outputs" / "CASE_009"
    # The OCR record exists for segmentation to read; no classification does.
    assert (out_dir / "ocr_result_DOC_001.json").exists()
    assert not (out_dir / "classification_result_DOC_001.json").exists()


def test_bundle_ocr_mode_writes_no_classification(tmp_path, monkeypatch):
    """What --bundle-ocr actually withholds is the document_type, not the read.

    `document_type` is a per-document value, so one label cannot be right for a
    bundle mixing a 진단서, a 검사 판독지 and a 진료비 명세서. That is the whole
    content of the flag now that reading a bundle is unremarkable.
    """
    _pending_bundle_manifest(tmp_path)
    _mock_ocr(monkeypatch, [("bundle page one", "bundle page one b", "agreed")])
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("a bundle must never be classified as one document"))

    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260721_001",
        classify=False, reader_a=object(), reader_b=object(), comparator=object())

    assert result["status"] == "bundle_ocr_complete"
    out_dir = tmp_path / "outputs" / "CASE_009"
    assert (out_dir / "ocr_result_DOC_001.json").exists()
    assert not (out_dir / "classification_result_DOC_001.json").exists()


def test_bundle_ocr_mode_still_refuses_a_superseded_bundle(tmp_path, monkeypatch):
    """After the split the parent is off-limits, even in bundle-OCR mode.

    The same document is the ONLY valid OCR target before the split and a
    forbidden one after it; downstream_disposition is what separates the two,
    and it is set by the split itself.
    """
    out_dir = tmp_path / "outputs" / "CASE_009"
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": "CASE_009", "created_at": dao.now_iso(),
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 1000, "ocr_status": "not_applicable",
            "segmentation_status": "completed",
            "downstream_disposition": "superseded_bundle",
        }],
    })
    monkeypatch.setattr(
        rc1, "run_ocr",
        lambda *a, **k: pytest.fail("a superseded bundle must never be re-OCR'd"),
    )
    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_001", "missing.pdf", "tester", "RUN_20260721_001",
        classify=False,
        reader_a=object(), reader_b=object(), comparator=object(),
    )
    assert result["status"] == "blocked_segmentation"


def test_resolving_a_bundles_disagreement_does_not_classify_it(tmp_path, monkeypatch):
    """P8 resolution must not smuggle in the classification --bundle-ocr withheld.

    Found on the real CASE_909 run: DOC_005's bundle OCR correctly wrote no
    document_type, then resolving its 8 disagreed pages classified the whole
    19-page bundle as `diagnosis_certificate` -- its first page's type, applied
    to a bundle that also holds two imaging REPORTs, an 입퇴원확인서 and two
    진료비 명세서. That is precisely the failure the flag exists to prevent, and
    the resolve path reached the shared tail without the flag.
    """
    _pending_bundle_manifest(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    proc = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "page_001.md").write_text("bundle page one", encoding="utf-8")
    dao.atomic_write_json(out_dir / "ocr_result_DOC_001.json", {
        "case_id": "CASE_009", "run_id": "RUN_20260721_001",
        "component": "document-pipeline", "status": "success",
        "created_at": dao.now_iso(),
        "model_info": {"model_name": "reader_a=x; reader_b=x", "prompt_version": "ocr_extraction_v0.1"},
        "document_id": "DOC_001", "ocr_engine": "x", "vision_model_name": "y",
        "uncertain_confidence_threshold": 1.0, "extraction_method": "ocr",
        "ocr_status": "completed", "ocr_quality": "low",
        "cross_validation_status": "disagreed_pending_review",
        "cross_validation_mode": "single_technology_weak_p8_poc",
        "cross_validation_note": "test", "review_required": True,
        "pages": [{
            "page": 1, "text_path": None, "mean_confidence": None,
            "uncertain_regions": [],
            "cross_validation": {"agreement": "disagreed", "vision_model_reading": "b",
                                  "disagreement_details": ["DISAGREE: mock"]},
        }],
    })
    raw_ocr = {
        "document_path": "fake.pdf",
        "pages": [{"page": 1, "reading_a": "bundle page one", "reading_b": "b",
                   "agreement": "disagreed", "disagreement_details": ["x"]}],
    }
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("a bundle must never be classified as one document"))

    result = rc1.resolve_from_raw_ocr(
        "CASE_009", "DOC_001", raw_ocr, page=1, chosen_reading="reading_a",
        resolved_by="tester", note="verification", held_by="document-pipeline",
        run_id="RUN_20260721_001", classify=False)

    assert result["status"] == "bundle_ocr_complete"
    assert not (out_dir / "classification_result_DOC_001.json").exists()


# --- classify an already-read document -----------------------------------------
#
# A split child inherits its pages from the bundle, so by the time it needs a
# document_type its text already exists. Before this there was no way to say
# "classify what is here": the only entry point was `run`, which starts by
# OCR'ing. Calling it on a child re-read pages that had just been redistributed
# and overwrote their P8 history with a fresh verdict -- which is what happened
# to CASE_909's DOC_006-013.


def _child_with_inherited_pages(tmp_path, doc_id="DOC_006"):
    out_dir = tmp_path / "outputs" / "CASE_009"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = tmp_path / "data" / "processed" / "CASE_009" / doc_id
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "page_001.md").write_text("진 단 서\n환자의 성명", encoding="utf-8")
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": "CASE_009", "created_at": dao.now_iso(),
        "documents": [{
            "document_id": doc_id, "file_name": f"{doc_id}.pdf",
            "file_path": f"data/raw/CASE_009/{doc_id}.pdf", "file_format": "pdf",
            "file_size_bytes": 100, "ocr_status": "completed",
            "segmentation_status": "completed", "source_file_name": "DOC_005.pdf",
            "source_page_start": 1, "source_page_end": 1,
            "segmentation_proposal_path":
                "outputs/CASE_009/segmentation_proposal_DOC_005.json",
        }],
    })
    dao.atomic_write_json(out_dir / f"ocr_result_{doc_id}.json", {
        "case_id": "CASE_009", "run_id": "RUN_20260805_001",
        "component": "document-pipeline", "status": "success",
        "created_at": dao.now_iso(),
        "model_info": {"model_name": "reader_a=x; reader_b=x",
                        "prompt_version": "ocr_extraction_v0.1"},
        "document_id": doc_id, "ocr_engine": "x", "vision_model_name": "y",
        "uncertain_confidence_threshold": 1.0, "extraction_method": "ocr",
        "ocr_status": "completed", "ocr_quality": "low",
        "cross_validation_status": "disagreed_resolved",
        "cross_validation_mode": "single_technology_weak_p8_poc",
        "cross_validation_note": "inherited from the bundle", "review_required": False,
        "pages": [{
            "page": 1, "text_path": f"data/processed/CASE_009/{doc_id}/page_001.md",
            "mean_confidence": None, "uncertain_regions": [],
            "cross_validation": {
                "agreement": "disagreed", "vision_model_reading": "b",
                "disagreement_details": ["DISAGREE: mock"],
                "resolution": {"chosen_reading": "reading_a", "resolved_by": "h",
                                "resolved_at": dao.now_iso(), "note": "n"},
            },
        }],
    })
    return out_dir


def test_classify_only_does_not_re_read_the_document(tmp_path, monkeypatch):
    """The inherited P8 record must survive classification untouched."""
    out_dir = _child_with_inherited_pages(tmp_path)
    monkeypatch.setattr(
        rc1, "run_ocr",
        lambda *a, **k: pytest.fail("classify-only must never re-OCR"))
    _mock_classify(monkeypatch, doc_type="diagnosis_certificate", label="진단서")

    result = rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    assert result["status"] == "passed"
    assert result["document_type"] == "diagnosis_certificate"
    ocr = json.loads((out_dir / "ocr_result_DOC_006.json").read_text(encoding="utf-8"))
    assert ocr["cross_validation_status"] == "disagreed_resolved", \
        "the inherited P8 history must not be replaced by a fresh read"
    assert ocr["pages"][0]["cross_validation"]["resolution"]["chosen_reading"] == "reading_a"


def test_running_full_checkpoint1_on_an_already_read_document_is_refused(tmp_path, monkeypatch):
    """The guard that would have prevented the CASE_909 overwrite.

    `run` starts by OCR'ing, so calling it on a document whose pages were
    inherited destroys exactly the record redistribution just created. Refuse
    it and name the alternative rather than silently paying twice.
    """
    _child_with_inherited_pages(tmp_path)
    monkeypatch.setattr(
        rc1, "run_ocr",
        lambda *a, **k: pytest.fail("must refuse before reaching OCR"))

    result = rc1.run_checkpoint1(
        "CASE_009", "DOC_006", "missing.pdf", "tester", "RUN_20260805_002",
        reader_a=object(), reader_b=object(), comparator=object(),
        classifier=object())

    assert result["status"] == "already_extracted"
    assert "classify-only" in result["next_action"]


def test_a_form_naming_title_classifies_without_calling_the_model(tmp_path, monkeypatch):
    """The split's own title decides the type when it names a form.

    The boundary was cut on that printed title at precision 1.0000, so asking a
    model to re-read the same page and name a type is a second opinion on
    evidence already held exactly. Verified against the model on CASE_909: all
    four titles it maps agreed with the classifier, none differed.
    """
    _child_with_inherited_pages(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    dao.atomic_write_json(out_dir / "segmentation_proposal_DOC_005.json", {
        "review_status": "approved", "method": {"mode": "text_anchor"},
        "segments": [{"page_start": 1, "page_end": 1,
                      "provisional_type_label": "진 단 서"}],
    })
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("a form-naming title needs no model call"))

    result = rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline", run_id="RUN_20260805_002")

    assert result["document_type"] == "diagnosis_certificate"
    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    assert written["classification_source"] == "printed_form_title"
    assert "진 단 서" in written["evidence_references"][0]["quote"]


def test_enabled_medical_routing_classifies_a_form_title_without_model(tmp_path, monkeypatch):
    _child_with_inherited_pages(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    dao.atomic_write_json(out_dir / "segmentation_proposal_DOC_005.json", {
        "review_status": "approved", "method": {"mode": "text_anchor"},
        "segments": [{"page_start": 1, "page_end": 1,
                      "provisional_type_label": "진 단 서"}],
    })
    _enable_medical_routing(monkeypatch)
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("a fine-grained form title needs no model call"))

    rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    medical = written["medical_classification"]
    assert medical["status"] == "deterministic_title"
    assert medical["kind"] == "diagnosis_certificate"
    assert medical["evidence_references"] == [{"page": 1, "quote": "진 단 서"}]


def test_enabled_medical_routing_marks_inherited_policy_without_fake_quote(tmp_path, monkeypatch):
    _child_with_inherited_pages(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    manifest_path = out_dir / "document_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"].insert(0, {
        "document_id": "DOC_005", "file_name": "DOC_005.pdf",
        "file_path": "data/raw/CASE_009/DOC_005.pdf",
        "file_format": "pdf", "file_size_bytes": 100,
        "ocr_status": "not_applicable", "segmentation_status": "completed",
        "segmentation_proposal_path":
            "outputs/CASE_009/segmentation_proposal_DOC_005.json",
        "downstream_disposition": "superseded_bundle",
    })
    dao.atomic_write_json(manifest_path, manifest)
    dao.atomic_write_json(out_dir / "segmentation_proposal_DOC_005.json", {
        "review_status": "approved", "method": {"mode": "text_anchor"},
        "segments": [{"page_start": 1, "page_end": 1,
                      "provisional_type_label": "상해보험 특별약관"}],
    })
    _enable_medical_routing(monkeypatch)
    monkeypatch.setattr(
        rc1, "classify_document",
        lambda *a, **k: pytest.fail("an inherited policy needs no model call"))

    rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    assert written["evidence_references"] == [{"page": 1, "quote": "상해보험 특별약관"}]
    medical = written["medical_classification"]
    assert medical["status"] == "not_medical"
    assert medical["evidence_references"] == [{"page": 1, "quote": "상해보험 특별약관"}]
    assert "classification response contained no quote" not in json.dumps(written)


def test_enabled_medical_routing_calls_model_once_for_ambiguous_title(tmp_path, monkeypatch):
    _child_with_inherited_pages(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    dao.atomic_write_json(out_dir / "segmentation_proposal_DOC_005.json", {
        "review_status": "approved", "method": {"mode": "text_anchor"},
        "segments": [{"page_start": 1, "page_end": 1,
                      "provisional_type_label": "REPORT"}],
    })
    config = _enable_medical_routing(monkeypatch)
    calls = []

    def fake_classify(text, classifier=None, routing_config=None):
        calls.append(text)
        assert routing_config is config
        return {
            "predicted_document_type": "imaging_report",
            "document_type_label": "영상판독지",
            "confidence": 0.9,
            "quote": "진 단 서",
            "medical_document_kind": "imaging_interpretation",
            "medical_kind_candidates": [],
            "medical_ambiguity_reason": None,
        }

    monkeypatch.setattr(rc1, "classify_document", fake_classify)
    rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    assert len(calls) == 1
    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    assert written["medical_classification"]["status"] == "llm_classified"
    assert written["medical_classification"]["kind"] == "imaging_interpretation"


def test_a_genre_naming_title_still_calls_the_model(tmp_path, monkeypatch):
    """"REPORT" names a genre, so the page still has to be read."""
    _child_with_inherited_pages(tmp_path)
    out_dir = tmp_path / "outputs" / "CASE_009"
    dao.atomic_write_json(out_dir / "segmentation_proposal_DOC_005.json", {
        "review_status": "approved", "method": {"mode": "text_anchor"},
        "segments": [{"page_start": 1, "page_end": 1,
                      "provisional_type_label": "REPORT"}],
    })
    _mock_classify(monkeypatch, doc_type="imaging_report", label="영상판독지")

    result = rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline", run_id="RUN_20260805_002")

    assert result["document_type"] == "imaging_report"


def test_classification_reads_the_redacted_text_when_it_exists(tmp_path, monkeypatch):
    """Classification must prefer redacted text over the raw page.

    `page_NNN.md` still carries claimant PII -- dao.read-page-text guards it
    behind checkpoint 2's one-shot capability precisely so no analysis stage
    reads it -- and classification reaching it by following ocr_result's
    text_path bypassed that. A split child inherits the bundle's redaction, so
    the redacted text is normally there.
    """
    out_dir = _child_with_inherited_pages(tmp_path)
    proc = tmp_path / "data" / "processed" / "CASE_009" / "DOC_006"
    (proc / "page_001.md").write_text("환자 홍길동 진 단 서", encoding="utf-8")
    (proc / "redacted_text.md").write_text(
        "<<<PAGE page=1>>>\n환자 [REDACTED] 진 단 서\n", encoding="utf-8")
    seen = {}

    def fake_classify(text, classifier=None):
        seen["text"] = text
        return {"predicted_document_type": "diagnosis_certificate",
                "document_type_label": "진단서", "confidence": 0.9, "quote": text[:20]}

    monkeypatch.setattr(rc1, "classify_document", fake_classify)

    result = rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    assert "[REDACTED]" in seen["text"]
    assert "홍길동" not in seen["text"]
    assert result["classification_text_source"] == "redacted_text"
    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    assert written.get("classification_text_source") == "redacted_text"


def test_falling_back_to_raw_page_text_is_recorded(tmp_path, monkeypatch):
    """The fallback stays, but never silently.

    A document redacted after classification (or one whose redaction failed)
    still has to be classifiable. What must not happen is reading raw PII
    without that being visible afterwards, so the source is recorded on the
    contract either way.
    """
    out_dir = _child_with_inherited_pages(tmp_path)
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")

    result = rc1.classify_existing(
        "CASE_009", "DOC_006", held_by="document-pipeline",
        run_id="RUN_20260805_002")

    assert result["classification_text_source"] == "raw_page_text"
    written = json.loads(
        (out_dir / "classification_result_DOC_006.json").read_text(encoding="utf-8"))
    assert written["classification_text_source"] == "raw_page_text"
    assert written["review_required"] is True, \
        "reading unredacted text is a fact a reviewer should see"


def test_page_range_slice_preserves_the_text_layer(tmp_path):
    """A sliced page must extract the same text its source page does.

    Regression: _page_range_pdf used insert_pdf(), which rebuilds each page's
    resources into a fresh document and drops the glyphs of a page whose text
    is drawn in a subset TrueType font with a mislabelled WinAnsi encoding --
    what Korean insurer PDFs use ("ABCDEE+바탕체"). A page extracting 0 chars
    reads as a genuine scan to ocr_extract's embedded-text check, which routes
    the whole document to vision OCR. Nothing fails loudly; the symptoms are
    cost and a quality downgrade on the path where vision has been observed
    hallucinating an insurer slogan.

    segment_case.split_bundle fixed exactly this in its own slicer and measured
    it (CASE_905, 323 pages: insert_pdf lost 3 cover pages, select lost 0).
    This slicer -- the other place the repo cuts a PDF -- was never updated.
    Re-measured on CASE_902 DOC_001 (249p, 248 with text): insert_pdf lost
    pages 2-7 and 248; select lost none and reproduced every char count.

    NOTE on fixture fidelity, same as test_segment_case's equivalent: a
    synthetic PyMuPDF document does not reproduce the font corruption -- both
    slicers keep its text -- so this asserts the invariant (slice text ==
    source text) rather than proving the old implementation fails. The measured
    evidence lives on the real PDFs named above.
    """
    fitz = pytest.importorskip("fitz")

    pdf_path = tmp_path / "textful.pdf"
    with fitz.open() as doc:
        for i in range(6):
            page = doc.new_page()
            page.insert_text((72, 100), f"PAGE {i + 1} CONTENT", fontsize=14)
        doc.save(pdf_path)

    with fitz.open(pdf_path) as doc:
        source = [doc[i].get_text().strip() for i in range(6)]
    assert all(source), "fixture must have text on every page"

    with rc1._page_range_pdf(pdf_path, "CASE_009", "DOC_001", 2, 5) as sliced:
        with fitz.open(sliced) as d:
            assert d.page_count == 4
            got = [d[i].get_text().strip() for i in range(4)]

    assert got == source[1:5], (
        "the slice must reproduce its source pages' text exactly")


def test_page_range_slice_does_not_mutate_the_raw_source(tmp_path):
    """select() mutates the document it is called on, so the slicer must open
    its own handle. P2 makes raw input immutable, and a slicer that wrote back
    to data/raw would be the worst possible way to break it."""
    fitz = pytest.importorskip("fitz")

    pdf_path = tmp_path / "source.pdf"
    with fitz.open() as doc:
        for i in range(5):
            page = doc.new_page()
            page.insert_text((72, 100), f"PAGE {i + 1}", fontsize=14)
        doc.save(pdf_path)
    before = pdf_path.read_bytes()

    with rc1._page_range_pdf(pdf_path, "CASE_009", "DOC_001", 2, 3) as sliced:
        assert sliced != pdf_path

    assert pdf_path.read_bytes() == before, "the raw source PDF was modified"
    with fitz.open(pdf_path) as doc:
        assert doc.page_count == 5, "the raw source lost pages"
