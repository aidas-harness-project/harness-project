"""intake_case.py -- coverage beyond test_intake_content_scan.py's D2
content-check tests: the pure planning/parsing helpers (classify,
parse_split_spec, split_output_name, file_format_for), write_manifest's
DAO-backed write, and the --execute path's DOC_XXX/GT_XXX rename +
manifest-write + _intake_record.json crosswalk end to end (known-gaps.md
item 4: this path had zero coverage before this file).

No real provider calls -- most tests never touch --init-ledger's content
pre-check (that's test_intake_content_scan.py's job); ledgers here are
written directly, already resolved, to exercise --execute.
"""
import json

import pytest

import dao
import intake_case


@pytest.fixture
def isolated_intake(tmp_path, monkeypatch):
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    monkeypatch.setattr(intake_case, "ROOT", tmp_path)
    return tmp_path


class FakeFile:
    def __init__(self, name):
        self.name = name


# --------------------------------------------------------- classify() --

def test_classify_splits_raw_and_ground_truth_by_pattern():
    files = [FakeFile("claim_form.pdf"), FakeFile("최종손해사정서.pdf"), FakeFile("diagnosis.pdf")]
    plan = intake_case.classify(files, intake_case.DEFAULT_GT_PATTERNS)

    dest_by_name = {f.name: dest for f, dest in plan}
    assert dest_by_name["claim_form.pdf"] == "raw"
    assert dest_by_name["최종손해사정서.pdf"] == "ground_truth"
    assert dest_by_name["diagnosis.pdf"] == "raw"


def test_classify_ignores_os_junk_files():
    files = [FakeFile(".DS_Store"), FakeFile("Thumbs.db"), FakeFile("real.pdf")]
    plan = intake_case.classify(files, intake_case.DEFAULT_GT_PATTERNS)

    assert [f.name for f, _ in plan] == ["real.pdf"]


# ---------------------------------------------------- parse_split_spec() --

def test_parse_split_spec_valid():
    fname, ranges = intake_case.parse_split_spec("doc.pdf:1-13=ground_truth,14-110=raw")

    assert fname == "doc.pdf"
    assert ranges == [(1, 13, "ground_truth"), (14, 110, "raw")]


def test_parse_split_spec_rejects_overlapping_ranges():
    with pytest.raises(SystemExit):
        intake_case.parse_split_spec("doc.pdf:1-13=raw,10-20=ground_truth")


def test_parse_split_spec_rejects_bad_tier():
    with pytest.raises(SystemExit):
        intake_case.parse_split_spec("doc.pdf:1-13=not_a_real_tier")


def test_parse_split_spec_rejects_missing_colon():
    with pytest.raises(SystemExit):
        intake_case.parse_split_spec("doc.pdf-1-13=raw")


def test_parse_split_spec_rejects_backwards_range():
    with pytest.raises(SystemExit):
        intake_case.parse_split_spec("doc.pdf:13-1=raw")


# ------------------------------------------------------------ formatting --

def test_split_output_name_zero_pads_page_numbers():
    from pathlib import Path
    name = intake_case.split_output_name(Path("doc.pdf"), 1, 13)
    assert name == "doc__p001-013.pdf"


@pytest.mark.parametrize("ext,expected", [
    (".pdf", "pdf"), (".PDF", "pdf"), (".png", "image"), (".jpg", "image"),
    (".txt", "text"), (".xlsx", "spreadsheet"), (".csv", "spreadsheet"), (".weird", "other"),
])
def test_file_format_for_known_and_unknown_extensions(ext, expected):
    assert intake_case.file_format_for(ext) == expected


# ------------------------------------------------------------- write_manifest --

def test_write_manifest_writes_a_schema_valid_file(isolated_intake):
    documents = [{
        "document_id": "DOC_001", "file_name": "DOC_001.pdf",
        "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "pre_flagged_type": None, "pages": None, "ocr_status": "pending",
        "ocr_text_path": None, "ocr_quality": None, "uncertain_region_count": None,
        "cross_validation_status": None, "redacted_text_path": None,
        "document_type": None, "classification_confidence": None,
    }]

    target = intake_case.write_manifest("CASE_009", "RUN_20260713_001", documents)

    assert target.exists()
    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert manifest["documents"][0]["document_id"] == "DOC_001"
    assert dao.read_lock(target) is None


def test_write_manifest_schema_failure_exits_without_writing(isolated_intake):
    bad_documents = [{"document_id": "DOC_001"}]  # missing every other required field

    with pytest.raises(SystemExit):
        intake_case.write_manifest("CASE_009", "RUN_20260713_001", bad_documents)

    target = dao.case_dir("CASE_009") / "document_manifest.json"
    assert not target.exists()


# ------------------------------------------------------------- main() --execute --

def _make_source_case(tmp_path, name="test-case"):
    src = tmp_path / "source-cases" / name
    src.mkdir(parents=True)
    (src / "claim_form_kim.pdf").write_bytes(b"%PDF-1.4 raw content A")
    (src / "diagnosis_kim.pdf").write_bytes(b"%PDF-1.4 raw content B")
    (src / "최종손해사정서.pdf").write_bytes(b"%PDF-1.4 ground truth content")
    return src


def _write_approved_ledger(case_id, source_dir, files_and_status):
    """files_and_status: list of (file_name, classification, review_status).
    Bypasses --init-ledger's real content-scan provider call entirely --
    these tests exercise --execute, not D2's content check (that's
    test_intake_content_scan.py's job)."""
    entries = []
    for file_name, classification, status in files_and_status:
        entries.append({
            "file_name": file_name, "classification": classification, "review_status": status,
            "reviewed_by": "tester" if status != "pending" else None,
            "reviewed_at": dao.now_iso() if status != "pending" else None,
            "rejection_reason": "test rejection" if status == "rejected" else None,
        })
    ledger = {"case_id": case_id, "source_dir": str(source_dir), "created_at": dao.now_iso(),
              "updated_at": dao.now_iso(), "files": entries}
    dao.atomic_write_json(dao.source_ledger_path(case_id), ledger)


def _run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["intake_case.py"] + argv)
    intake_case.main()


def test_execute_renames_files_to_sequential_doc_and_gt_ids(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])

    _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001"])

    raw_dir = isolated_intake / "data" / "raw" / "CASE_009"
    gt_dir = isolated_intake / "data" / "ground_truth" / "CASE_009"
    assert sorted(p.name for p in raw_dir.glob("DOC_*.pdf")) == ["DOC_001.pdf", "DOC_002.pdf"]
    assert sorted(p.name for p in gt_dir.glob("GT_*.pdf")) == ["GT_001.pdf"]
    # Original PII-bearing filenames must not survive into data/raw or data/ground_truth
    assert not (raw_dir / "claim_form_kim.pdf").exists()
    assert (raw_dir / "DOC_001.pdf").read_bytes() == b"%PDF-1.4 raw content A"


def test_execute_writes_manifest_matching_the_renamed_files(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])

    _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001"])

    manifest = json.loads((isolated_intake / "outputs" / "CASE_009" / "document_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 2, "ground-truth file must never appear in document_manifest.json"
    doc_ids = {d["document_id"] for d in manifest["documents"]}
    assert doc_ids == {"DOC_001", "DOC_002"}
    for d in manifest["documents"]:
        assert d["ocr_status"] == "pending"
        assert d["segmentation_status"] == "pending_review"
        assert d["segmentation_reviewed_by"] is None
        assert d["file_path"].startswith("data/raw/CASE_009/")


def test_execute_writes_intake_record_crosswalk(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])

    _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001"])

    record = json.loads((isolated_intake / "data" / "raw" / "CASE_009" / "_intake_record.json").read_text(encoding="utf-8"))
    raw_names = {e["original_file_name"] for e in record["raw"]}
    assert raw_names == {"claim_form_kim.pdf", "diagnosis_kim.pdf"}, \
        "the crosswalk is the only place original (potentially PII-bearing) filenames survive intake"
    gt_names = {e["original_file_name"] for e in record["ground_truth"]}
    assert gt_names == {"최종손해사정서.pdf"}


def test_execute_blocked_when_any_file_rejected(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "rejected"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001"])

    raw_dir = isolated_intake / "data" / "raw" / "CASE_009"
    assert not raw_dir.exists() or not any(raw_dir.glob("DOC_*.pdf")), \
        "D2: a single rejected file blocks the whole case -- not even already-approved files copy"


def test_execute_blocked_when_ledger_has_pending_entries(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "pending"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001"])


def test_execute_without_ledger_exits(isolated_intake, monkeypatch):
    src = _make_source_case(isolated_intake)

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [str(src), "CASE_009", "--execute"])


def test_execute_uses_the_dao_validated_source_ledger_gate(
    isolated_intake, monkeypatch
):
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])
    monkeypatch.setattr(
        dao,
        "validated_source_ledger",
        lambda _case_id: (_ for _ in ()).throw(ValueError("tampered receipt")),
        raising=False,
    )

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            str(src), "CASE_009", "--execute", "--run-id", "RUN_20260713_001",
        ])
    assert not (isolated_intake / "data" / "raw" / "CASE_009").exists()


def test_dry_run_does_not_write_anything(isolated_intake, monkeypatch, capsys):
    src = _make_source_case(isolated_intake)

    _run_main(monkeypatch, [str(src), "CASE_009"])

    assert not (isolated_intake / "outputs").exists()
    assert not (isolated_intake / "data").exists()
    assert "dry run" in capsys.readouterr().out


def test_init_ledger_writes_every_raw_entry_pending_with_no_scan(
    isolated_intake, monkeypatch
):
    """D2's gate, after the vision pre-check was removed (2026-08-20).

    This replaces two tests that drove the scan itself. What they were really
    protecting is asserted here and is unchanged: every raw entry lands
    `pending` for a human, and no `content_warning` is invented now that
    nothing scans. The removal is asserted separately, in
    test_poc_guardrail_scope.py.
    """
    src = _make_source_case(isolated_intake)

    _run_main(monkeypatch, [str(src), "CASE_009", "--init-ledger"])

    ledger = json.loads((isolated_intake / "outputs" / "CASE_009" /
                         "_source_ledger.json").read_text(encoding="utf-8"))
    raw_entries = [entry for entry in ledger["files"]
                   if entry["classification"] == "raw"]
    assert raw_entries, "test setup should include raw-proposed PDFs"
    assert all(entry["review_status"] == "pending" for entry in raw_entries)
    assert all("content_warning" not in entry for entry in raw_entries)


def test_execute_blocks_when_classification_drifts_from_ledger(isolated_intake, monkeypatch):
    """Fleet D1 fix: a file reviewed+approved as ground_truth must never be
    copied to data/raw because --execute was run with different args than
    --init-ledger. The guard blocks the whole case rather than contaminate raw."""
    src = _make_source_case(isolated_intake)
    _write_approved_ledger("CASE_009", src, [
        ("claim_form_kim.pdf", "raw", "approved"),
        ("diagnosis_kim.pdf", "raw", "approved"),
        ("최종손해사정서.pdf", "ground_truth", "approved"),
    ])
    # Execute with a --ground-truth pattern matching nothing -> the fresh
    # classification would route the answer key to data/raw. Guard must block.
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [str(src), "CASE_009", "--execute", "--run-id",
                                "RUN_20260713_001", "--ground-truth", "*does-not-match*"])
    raw_dir = isolated_intake / "data" / "raw" / "CASE_009"
    assert not raw_dir.exists() or not list(raw_dir.glob("*"))


# ------------------------------------ adjuster-supplied case type (#8/#8a) --

def _case_type_args(**kw):
    """A stand-in for the parsed argparse namespace, carrying only the fields
    build_adjuster_case_type reads."""
    class _Args:
        pass
    args = _Args()
    for field in ("coverage_basis", "loss_type", "supplied_by", "case_type_note"):
        setattr(args, field, kw.get(field))
    args.non_claim_case = kw.get("non_claim_case", False)
    return args


def test_axis_values_come_from_the_schema_not_a_copy():
    """The CLI's accepted values are derived from case_type_result.schema.json
    rather than restated. A hand-copied list is exactly the drift shape that
    hit the forbidden-expression table (CLAUDE.md, 2026-07-17)."""
    assert intake_case.COVERAGE_BASIS_VALUES == ["배상책임", "개인보험", "자동차보험"]
    assert intake_case.LOSS_TYPE_VALUES == ["후유장해", "진단·수술비", "실손"]
    assert None not in intake_case.COVERAGE_BASIS_VALUES


def test_build_adjuster_case_type_returns_none_when_nothing_supplied():
    assert intake_case.build_adjuster_case_type(_case_type_args()) is None


def test_build_adjuster_case_type_accepts_both_axes():
    result = intake_case.build_adjuster_case_type(_case_type_args(
        coverage_basis="자동차보험", loss_type="후유장해", supplied_by="Kim TY"))

    assert result["coverage_basis"] == "자동차보험"
    assert result["loss_type"] == "후유장해"
    assert result["is_claim_case"] is True
    assert result["supplied_by"] == "Kim TY"
    assert "supplied_at" in result


@pytest.mark.parametrize("kwargs", [
    {"coverage_basis": "배상책임", "supplied_by": "Kim TY"},
    {"loss_type": "후유장해", "supplied_by": "Kim TY"},
])
def test_half_classified_claim_is_rejected(kwargs):
    """Both axes or neither: one alone would let the draft stage pick a
    template from half a classification."""
    with pytest.raises(SystemExit):
        intake_case.build_adjuster_case_type(_case_type_args(**kwargs))


def test_case_type_without_supplier_is_rejected():
    """supplied_by is the provenance standing in for the document quote P1
    would otherwise require -- no document states a case's type."""
    with pytest.raises(SystemExit):
        intake_case.build_adjuster_case_type(_case_type_args(
            coverage_basis="배상책임", loss_type="후유장해"))


def test_supplier_without_a_case_type_is_rejected():
    with pytest.raises(SystemExit):
        intake_case.build_adjuster_case_type(_case_type_args(supplied_by="Kim TY"))


def test_non_claim_case_nulls_both_axes():
    result = intake_case.build_adjuster_case_type(_case_type_args(
        non_claim_case=True, supplied_by="Kim TY"))

    assert result["is_claim_case"] is False
    assert result["coverage_basis"] is None
    assert result["loss_type"] is None


def test_non_claim_case_cannot_also_carry_an_axis():
    with pytest.raises(SystemExit):
        intake_case.build_adjuster_case_type(_case_type_args(
            non_claim_case=True, coverage_basis="배상책임", supplied_by="Kim TY"))


def test_non_claim_case_still_needs_a_supplier():
    with pytest.raises(SystemExit):
        intake_case.build_adjuster_case_type(_case_type_args(non_claim_case=True))


def test_loss_type_without_a_template_warns_but_is_accepted(capsys):
    """실손 has no templates/registry.json contract, so its draft cannot
    render. Intake records it and says so now, rather than failing silently
    at draft time (open-decisions.md #8a)."""
    result = intake_case.build_adjuster_case_type(_case_type_args(
        coverage_basis="개인보험", loss_type="실손", supplied_by="Kim TY"))

    assert result["loss_type"] == "실손"
    assert "no templates/registry.json contract" in capsys.readouterr().err


def _manifest_documents():
    return [{
        "document_id": "DOC_001", "file_name": "DOC_001.pdf",
        "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "pre_flagged_type": None, "pages": None, "ocr_status": "pending",
        "ocr_text_path": None, "ocr_quality": None, "uncertain_region_count": None,
        "cross_validation_status": None, "redacted_text_path": None,
        "document_type": None, "classification_confidence": None,
    }]


def test_manifest_omits_the_field_entirely_when_no_case_type_supplied(isolated_intake):
    """CASE_907 compatibility: a manifest written without an adjuster case
    type must be indistinguishable from one written before the field existed."""
    target = intake_case.write_manifest("CASE_009", "RUN_20260804_001", _manifest_documents())

    assert "adjuster_case_type" not in json.loads(target.read_text(encoding="utf-8"))


def test_manifest_carries_the_adjuster_case_type_when_supplied(isolated_intake):
    supplied = intake_case.build_adjuster_case_type(_case_type_args(
        coverage_basis="개인보험", loss_type="후유장해", supplied_by="Kim TY",
        case_type_note="개인보험 series"))

    target = intake_case.write_manifest(
        "CASE_009", "RUN_20260804_001", _manifest_documents(), supplied)

    stored = json.loads(target.read_text(encoding="utf-8"))["adjuster_case_type"]
    assert stored["coverage_basis"] == "개인보험"
    assert stored["loss_type"] == "후유장해"
    assert stored["note"] == "개인보험 series"


def test_manifest_rejects_a_half_classified_case_type(isolated_intake):
    """The schema is the backstop for anything that bypasses the CLI helper."""
    with pytest.raises(SystemExit):
        intake_case.write_manifest("CASE_009", "RUN_20260804_001", _manifest_documents(), {
            "coverage_basis": "배상책임", "loss_type": None,
            "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
        })


def test_manifest_rejects_a_non_claim_case_carrying_an_axis(isolated_intake):
    with pytest.raises(SystemExit):
        intake_case.write_manifest("CASE_009", "RUN_20260804_001", _manifest_documents(), {
            "coverage_basis": "배상책임", "loss_type": "후유장해", "is_claim_case": False,
            "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
        })


def test_manifest_rejects_an_unknown_coverage_basis(isolated_intake):
    with pytest.raises(SystemExit):
        intake_case.write_manifest("CASE_009", "RUN_20260804_001", _manifest_documents(), {
            "coverage_basis": "산재보험", "loss_type": "후유장해",
            "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
        })
